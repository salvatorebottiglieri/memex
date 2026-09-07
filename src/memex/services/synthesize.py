"""SynthesizerService — orchestration for synthesis operations.

Encapsulates: parent validation, content loading, idempotency checks,
agent synthesis, file writing, node/edge creation, confidence assignment,
deterministic checks (D1–D6), post-creation LLM validations (V1–V2), and
trust state updates.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memex.agent import Agent
from memex.rules import canonicalize_synthesis_markers
from memex.schemas import DocumentRef, coerce_derivation
from memex.store import Store, min_confidence
from memex.utils.retry import call_with_retry
from memex.validators.validate import (
    merge_gate_failures,
    run_validations,
    validation_environment,
)


def _first_h1(content: str) -> str | None:
    """First ``# Heading`` line of *content*, or None."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _resolve_link_keys(prose: str, key_map: dict[str, str]) -> str:
    """Rewrite stable parent keys to their resolved link targets.

    The agent references parents by change-insensitive keys (``[[P1|A]]``);
    the system inserts the real link target (``[[filename|A]]``)
    deterministically, so the agent never emits a fragile uuid/path.
    Links whose target is not a key are left untouched.
    """
    def _repl(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        alias = (match.group(2) or "").strip()
        resolved = key_map.get(target)
        if resolved is None:
            return match.group(0)
        return f"[[{resolved}|{alias}]]" if alias else f"[[{resolved}]]"

    return _LINK_RE.sub(_repl, prose)


class SynthesizerService:
    """Orchestrate synthesis operations behind a small interface.

    Callers provide dependencies via constructor, then call ``synthesize()``.
    """

    def __init__(self, store: Store, vault_path: Path, agent: Agent) -> None:
        self._store = store
        self._vault_path = vault_path
        self._agent = agent
        # Validation judge + enabled flag: shared with derive (single source
        # of truth in validators.validate.validation_environment).
        self._judge, self._validations_enabled = validation_environment(agent)

    def synthesize(
        self, parent_ids: list[str]
    ) -> dict:
        """Synthesize across *parent_ids*.

        Validates all parents exist, checks idempotency by parent set,
        runs agent synthesis, writes markdown, creates the synthesis node
        and provenance edges, sets confidence, runs content checks
        (D1–D6) and the post-creation LLM validations (V1–V2), and
        updates trust state.

        Returns a result dict (never raises — agent failures are captured
        in the result).
        """
        # --- Idempotency check ---
        existing = self._store.find_synthesis_by_parents(parent_ids)
        if existing is not None:
            return {
                "id": existing["id"],
                "status": "already_synthesized",
                "parent_ids": list(parent_ids),
            }

        # --- Validate parents ---
        max_depth = 0
        contents: list[str] = []
        references: list[DocumentRef] = []
        source_lines: list[str] = []
        key_map: dict[str, str] = {}
        for idx, pid in enumerate(parent_ids, start=1):
            parent = self._store.get_node(pid)
            if parent is None:
                return {
                    "status": "error",
                    "detail": f"parent node not found: {pid}",
                    "parent_ids": list(parent_ids),
                }
            max_depth = max(max_depth, parent["depth"])
            content_path = parent.get("content_path") or ""
            filename = Path(content_path).stem if content_path else parent["id"]
            key = f"P{idx}"
            key_map[key] = filename
            content_text = ""
            if content_path and Path(content_path).exists():
                content_text = Path(content_path).read_text(encoding="utf-8")
                contents.append(content_text)
                if getattr(self._agent, "can_read_files", False):
                    references.append(
                        DocumentRef(
                            node_id=parent["id"],
                            content_path=content_path,
                            title=parent.get("title"),
                            source_url=parent.get("source_url"),
                            size_bytes=Path(content_path).stat().st_size,
                            link_key=key,
                        )
                    )
            else:
                contents.append("")
            # Stable per-parent key: the agent references parents by P1..Pn
            # (change-insensitive); the system resolves the key to the real
            # link target after the agent returns. The agent never emits the
            # filename/uuid itself.
            alias = parent.get("title") or _first_h1(content_text) or parent["id"]
            source_lines.append(f"- [[{key}|{alias}]]")

        # Syntheses link every source-derived fact to its parent: give inline
        # agents the exact link targets up front.
        combined_content = (
            "# Sources\n"
            + "\n".join(source_lines)
            + "\n\n---\n\n"
            + "\n\n---\n\n".join(contents)
        )

        # --- Agent call ---
        def _agent_derive():
            kwargs = (
                {"reference": references}
                if references
                else {"content": combined_content}
            )
            return self._agent.derive(**kwargs)

        try:
            deriv = call_with_retry(_agent_derive)
            coerce_derivation(deriv)
            deriv.prose = _resolve_link_keys(deriv.prose, key_map)
        except Exception as e:
            return {
                "status": "error",
                "detail": str(e),
                "parent_ids": list(parent_ids),
            }

        deriv_id = str(uuid.uuid4())

        # --- Write markdown file ---
        self._vault_path.mkdir(parents=True, exist_ok=True)
        first_line = deriv.prose.split("\n")[0].strip()
        head_name = (
            first_line.lstrip("# ").strip().strip('"').strip("'") or deriv_id
        )
        md_path = self._human_path(head_name)
        # Ticket #143: same marker canonicalization as derive — the file
        # renders the column, so D3's file-vs-column check always passes.
        prose = canonicalize_synthesis_markers(
            deriv.prose, deriv.synthesis_statements
        )
        md_path.write_text(prose, encoding="utf-8")

        # --- Create node and provenance edges ---
        now = datetime.now(timezone.utc).isoformat()
        self._store.create_node(
            node_id=deriv_id,
            kind="summary",
            tier="synthesis",
            trust_state="draft",
            depth=max_depth + 1,
            content_path=str(md_path),
            created_at=now,
            synthesis_statements=deriv.synthesis_statements,
        )

        for pid in parent_ids:
            self._store.create_edge(
                edge_id=str(uuid.uuid4()),
                type="provenance",
                relation="derived_from",
                from_node=deriv_id,
                to_node=pid,
                written_by="llm",
            )

        # Synthesis: confidence = min(parents' confidence) — single source
        # of truth in store.min_confidence (all-high stays high).
        confidences: list[str] = []
        for pid in parent_ids:
            p = self._store.get_node(pid)
            if p and p.get("confidence"):
                confidences.append(p["confidence"])
        synth_conf = min_confidence(confidences)
        self._store._con.execute(
            "UPDATE node SET confidence = ? WHERE id = ?",
            (synth_conf, deriv_id),
        )

        # --- Content checks (D1–D6) ---
        from memex.checks import run_checks

        check_result = run_checks(self._store._con, deriv_id, md_path)

        # --- Adversarial validations (V1–V2, post-creation, always-on) ---
        validation_result = None
        if self._validations_enabled:
            validation_result = run_validations(
                self._judge, self._store._con, deriv_id, md_path
            )

        # One gate: D + V failures accumulate; any failure → draft.
        failures, trust_state = merge_gate_failures(
            check_result, validation_result
        )
        self._store.update_trust_state(
            node_id=deriv_id,
            trust_state=trust_state,
            check_failures=failures,
        )
        if validation_result is not None:
            self._store.update_evidence(deriv_id, validation_result.evidence)

        return {
            "id": deriv_id,
            "status": "synthesized",
            "parent_ids": list(parent_ids),
            "trust_state": trust_state,
            "content_path": str(md_path),
            "check_failures": failures,
        }

    def _human_path(self, name: str, suffix: str = ".md") -> Path:
        """Return a human-readable file path, appending a suffix on collision."""
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
