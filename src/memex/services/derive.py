"""DeriverService — orchestration for derive operations.

Encapsulates: content loading, idempotency checks, agent derivation,
adversarial validation, file writing, node/edge creation, confidence
assignment, checks, and trust state updates.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memex.agent import Agent, load_agent
from memex.schemas import DocumentRef, coerce_derivation
from memex.store import Store
from memex.utils.retry import call_with_retry
from memex.validators.validate import validate_derivation

# Upper bound on source content fed to the LLM. Extraction can produce
# multi-megabyte files (broken PDF text layers, HTML dumps) that blow past
# any model context window; the summary needs the head of the source, not
# a token overflow. Kept explicit so a truncated prompt is honest about it.
_MAX_PROMPT_CHARS = 120_000


def _cap_prompt_content(content: str) -> str:
    """Strip NUL bytes (PDF ToUnicode artifacts) and cap prompt size."""
    content = content.replace("\x00", "")
    if len(content) <= _MAX_PROMPT_CHARS:
        return content
    return (
        content[:_MAX_PROMPT_CHARS]
        + "\n\n[source content truncated — exceeds prompt limit; "
        "the remainder was not considered]"
    )


@dataclass
class DeriveResult:
    """Result of a single derive operation."""

    id: str
    status: str  # "derived" | "already_derived" | "quality_failed" | "error"
    l0_node_id: str
    trust_state: str | None = None
    content_path: str | None = None
    check_failures: list[str] | None = None
    reason: str | None = None
    detail: str | None = None


class DeriverService:
    """Orchestrate derivation operations behind a small interface.

    Callers provide dependencies via constructor, then call ``derive()``
    or ``derive_all()``.  The service owns content loading, idempotency,
    file I/O, database writes, and quality gates.
    """

    def __init__(self, store: Store, vault_path: Path, agent: Agent) -> None:
        self._store = store
        self._vault_path = vault_path
        self._agent = agent
        self._validator: Agent | None = None

        validator_path = os.environ.get("MEMEX_VALIDATOR")
        if validator_path:
            self._validator = load_agent(validator_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def derive(self, l0_node_id: str, *, use_retry: bool = False) -> DeriveResult:
        """Derive a single L0 node.

        Loads content from the vault, checks idempotency, runs the agent,
        validates, writes markdown, creates the node and provenance edge,
        sets confidence, runs content checks, and updates trust state.

        Returns a ``DeriveResult`` — never raises for agent failures
        (those are captured in the result status).
        """
        # --- Load the L0 node ---
        l0 = self._store.get_node(l0_node_id)
        if l0 is None:
            return DeriveResult(
                id=l0_node_id,
                status="error",
                l0_node_id=l0_node_id,
                detail="node_not_found",
            )

        if not l0.get("content_path") or not Path(
            l0["content_path"]
        ).exists():
            return DeriveResult(
                id=l0_node_id,
                status="error",
                l0_node_id=l0_node_id,
                detail="content_not_found",
            )

        content, reference = self._agent_inputs(l0)

        # --- Idempotency check ---
        existing = self._store.find_derived_from(l0_node_id)
        if existing is not None:
            return DeriveResult(
                id=existing["from_node"],
                status="already_derived",
                l0_node_id=l0_node_id,
            )

        return self._do_derive(
            l0_node_id, content, reference, use_retry=use_retry
        )

    def derive_all(self, limit: int | None = None) -> list[DeriveResult]:
        """Derive all un-derived L0 nodes, capped at *limit* when given.

        ``limit=None`` or ``limit <= 0`` means unlimited (derive everything).

        Returns results for already-derived L0s (status="already_derived")
        alongside newly derived ones.  Never raises; individual failures
        are captured per-node in the result list.
        """
        all_nodes = self._store.list_nodes()
        results: list[DeriveResult] = []
        seen_derived: set[str] = set()

        # Phase 1 — report already-derived L0s (extracted roots are the
        # content-bearing L0 of the url+extracted model; legacy raw_source
        # rows remain derivable during the transition).
        for node in all_nodes:
            if node.get("kind") not in ("raw_source", "extracted"):
                continue
            existing = self._store.find_derived_from(node["id"])
            if existing is not None:
                results.append(
                    DeriveResult(
                        id=node["id"],
                        status="already_derived",
                        l0_node_id=node["id"],
                    )
                )
                seen_derived.add(node["id"])

        # Phase 2 — derive un-derived L0s (same L0 set as phase 1)
        count = 0
        for node in all_nodes:
            if node.get("kind") not in ("raw_source", "extracted"):
                continue
            if node["id"] in seen_derived:
                continue
            if limit is not None and limit > 0 and count >= limit:
                break
            count += 1

            l0 = self._store.get_node(node["id"])
            if l0 is None or not l0.get("content_path"):
                continue

            try:
                content, reference = self._agent_inputs(l0)
                result = self._do_derive(
                    node["id"], content, reference, use_retry=True
                )
                results.append(result)
            except Exception as e:
                results.append(
                    DeriveResult(
                        id=node["id"],
                        status="error",
                        l0_node_id=node["id"],
                        detail=str(e),
                    )
                )

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _agent_inputs(self, l0: dict) -> tuple[str | None, DocumentRef | None]:
        """Decide what the agent receives for an L0.

        Reader agents (``can_read_files``) get a :class:`DocumentRef` and read
        the file themselves in multiple passes — no prompt cap, any length.
        Other agents get the (NUL-stripped, size-capped) inlined content.
        """
        content_path = Path(l0["content_path"])
        if getattr(self._agent, "can_read_files", False):
            return None, DocumentRef(
                node_id=l0["id"],
                content_path=str(content_path),
                title=l0.get("title"),
                source_url=l0.get("source_url"),
                size_bytes=content_path.stat().st_size,
            )
        content = _cap_prompt_content(
            content_path.read_text(encoding="utf-8")
        )
        return content, None

    def _do_derive(
        self,
        l0_node_id: str,
        l0_content: str | None,
        reference: DocumentRef | None,
        *,
        use_retry: bool = False,
    ) -> DeriveResult:
        """Core derivation pipeline (assumes caller owns idempotency)."""
        from memex.checks import run_checks

        def _agent_derive():
            kwargs = {"content": l0_content}
            if reference is not None:
                kwargs["reference"] = reference
            return self._agent.derive(**kwargs)

        try:
            deriv = (
                call_with_retry(_agent_derive)
                if use_retry
                else _agent_derive()
            )
            coerce_derivation(deriv)
        except Exception as e:
            return DeriveResult(
                id=l0_node_id,
                status="error",
                l0_node_id=l0_node_id,
                detail=str(e),
            )

        deriv_id = str(uuid.uuid4())

        # --- Adversarial validation gate ---
        if self._validator is not None:
            passes, warning = validate_derivation(
                self._validator, l0_content, deriv, reference=reference
            )
            if warning:
                import json as _json
                import sys as _sys

                _sys.stderr.write(
                    _json.dumps({"validator_warning": warning}) + "\n"
                )

            if not passes:
                return DeriveResult(
                    id=l0_node_id,
                    status="quality_failed",
                    l0_node_id=l0_node_id,
                    reason="Derivation does not meaningfully re-elaborate the source material.",
                )

        # --- Write markdown file ---
        self._vault_path.mkdir(parents=True, exist_ok=True)
        first_line = deriv.prose.split("\n")[0].strip()
        head_name = (
            first_line.lstrip("# ").strip().strip('"').strip("'") or deriv_id
        )
        md_path = self._human_path(head_name)
        md_path.write_text(deriv.prose, encoding="utf-8")

        # --- Create node and provenance edge ---
        now = datetime.now(timezone.utc).isoformat()
        parent = self._store.get_node(l0_node_id)
        parent_depth = parent["depth"] if parent else 0
        self._store.create_node(
            node_id=deriv_id,
            kind="summary",
            tier="notes",
            trust_state="draft",
            depth=parent_depth + 1,
            content_path=str(md_path),
            created_at=now,
            synthesis_statements=deriv.synthesis_statements,
        )
        self._store.create_edge(
            edge_id=str(uuid.uuid4()),
            type="provenance",
            relation="derived_from",
            from_node=deriv_id,
            to_node=l0_node_id,
            written_by="llm",
        )

        # Notes-tier with 1 parent → medium confidence
        self._store._con.execute(
            "UPDATE node SET confidence = 'medium' WHERE id = ?", (deriv_id,)
        )

        # --- Content checks ---
        check_result = run_checks(self._store._con, deriv_id, md_path)
        trust_state = (
            "auto-verified" if check_result.passed else "draft"
        )
        self._store.update_trust_state(
            node_id=deriv_id,
            trust_state=trust_state,
            check_failures=check_result.failures,
        )

        return DeriveResult(
            id=deriv_id,
            status="derived",
            l0_node_id=l0_node_id,
            trust_state=trust_state,
            content_path=str(md_path),
            check_failures=check_result.failures,
        )

    def _human_path(self, name: str, suffix: str = ".md") -> Path:
        """Return a human-readable file path, appending a suffix on collision.

        Mirrors ``cli._human_path``.
        """
        import re as _re

        safe = _re.sub(r"[^a-zA-Z0–9_\- ]", "", name).strip().lower()
        safe = _re.sub(r"\s+", "-", safe)[:80].rstrip("-")
        base = self._vault_path / (safe + suffix)
        if not base.exists():
            return base
        for i in range(2, 100):
            candidate = self._vault_path / f"{safe}-{i}{suffix}"
            if not candidate.exists():
                return candidate
        return base
