"""DeriverService — orchestration for derive operations.

Encapsulates: content loading, idempotency checks, agent derivation,
file writing, node/edge creation, confidence assignment, deterministic
checks (D1–D6), post-creation LLM validations (V1–V2), and trust state
updates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memex.agent import Agent
from memex.rules import MIN_CHARS, canonicalize_synthesis_markers
from memex.schemas import DocumentRef, coerce_derivation
from memex.store import Store
from memex.utils.parsing import _cap_prompt_content
from memex.utils.retry import call_with_retry
from memex.validators.validate import (
    merge_gate_failures,
    run_validations,
    validation_environment,
)


@dataclass
class DeriveResult:
    """Result of a single derive operation."""

    id: str
    # "derived" | "already_derived" | "no_content" | "error"
    status: str
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
        # Validation judge + enabled flag: shared with synthesize (single
        # source of truth in validators.validate.validation_environment).
        self._judge, self._validations_enabled = validation_environment(agent)

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

        # --- Real-content gate (ticket #141) ---
        # An L0 with no content (or content below the MIN_CHARS floor, e.g.
        # 55-byte frontmatter-only files) carries nothing to summarize: skip
        # the derivation — no node created, no LLM cost — instead of letting
        # the agent produce process-description notes.
        if self._below_min_chars(l0, content):
            return DeriveResult(
                id=l0_node_id,
                status="no_content",
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
                if self._below_min_chars(l0, content):
                    results.append(
                        DeriveResult(
                            id=node["id"],
                            status="no_content",
                            l0_node_id=node["id"],
                        )
                    )
                    continue
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

    def _below_min_chars(self, l0: dict, content: str | None) -> bool:
        """True when the L0 carries no real content (< MIN_CHARS, #141).

        Reader agents receive a DocumentRef and read the file themselves, so
        their content length comes from the file; other agents get the
        inlined content (already NUL-stripped and capped — exact for anything
        below MIN_CHARS, since the cap only trims far larger inputs).
        """
        if content is not None:
            return len(content) < MIN_CHARS
        content_path = l0.get("content_path")
        if not content_path or not Path(content_path).exists():
            return True
        return len(Path(content_path).read_text(encoding="utf-8")) < MIN_CHARS

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

        # --- Write markdown file ---
        self._vault_path.mkdir(parents=True, exist_ok=True)
        first_line = deriv.prose.split("\n")[0].strip()
        head_name = (
            first_line.lstrip("# ").strip().strip('"').strip("'") or deriv_id
        )
        md_path = self._human_path(head_name)
        # Ticket #143: the file renders the column — canonicalize its markers
        # from synthesis_statements so D3's exact file-vs-column check passes.
        prose = canonicalize_synthesis_markers(
            deriv.prose, deriv.synthesis_statements
        )
        md_path.write_text(prose, encoding="utf-8")

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

        # --- Content checks (D1–D6) ---
        check_result = run_checks(self._store._con, deriv_id, md_path)

        # --- Adversarial validations (V1–V2, post-creation, always-on) ---
        validation_result = None
        if self._validations_enabled:
            validation_result = run_validations(
                self._judge, self._store._con, deriv_id, md_path
            )

        # One gate: D + V failures accumulate into a single list; any failure
        # → draft. quality_failed is gone — status stays "derived" and the
        # annotations ride on trust_state=draft + check_failures.
        failures, trust_state = merge_gate_failures(
            check_result, validation_result
        )
        self._store.update_trust_state(
            node_id=deriv_id,
            trust_state=trust_state,
            check_failures=failures,
        )

        return DeriveResult(
            id=deriv_id,
            status="derived",
            l0_node_id=l0_node_id,
            trust_state=trust_state,
            content_path=str(md_path),
            check_failures=failures,
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
