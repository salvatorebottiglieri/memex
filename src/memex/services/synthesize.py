"""SynthesizerService — orchestration for synthesis operations.

Encapsulates: parent validation, content loading, idempotency checks,
agent synthesis, adversarial validation, file writing, node/edge
creation, confidence assignment, checks, and trust state updates.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memex.agent import Agent, load_agent
from memex.store import Store
from memex.utils.retry import call_with_retry
from memex.validators.validate import validate_derivation


class SynthesizerService:
    """Orchestrate synthesis operations behind a small interface.

    Callers provide dependencies via constructor, then call ``synthesize()``.
    """

    def __init__(self, store: Store, vault_path: Path, agent: Agent) -> None:
        self._store = store
        self._vault_path = vault_path
        self._agent = agent
        self._validator: Agent | None = None

        validator_path = os.environ.get("MEMEX_VALIDATOR")
        if validator_path:
            self._validator = load_agent(validator_path)

    def synthesize(
        self, parent_ids: list[str]
    ) -> dict:
        """Synthesize across *parent_ids*.

        Validates all parents exist, checks idempotency by parent set,
        runs agent synthesis, validates, writes markdown, creates the
        synthesis node and provenance edges, sets confidence, runs
        content checks, and updates trust state.

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
        for pid in parent_ids:
            parent = self._store.get_node(pid)
            if parent is None:
                return {
                    "status": "error",
                    "detail": f"parent node not found: {pid}",
                    "parent_ids": list(parent_ids),
                }
            max_depth = max(max_depth, parent["depth"])
            content_path = parent.get("content_path") or ""
            if content_path and Path(content_path).exists():
                contents.append(
                    Path(content_path).read_text(encoding="utf-8")
                )
            else:
                contents.append("")

        combined_content = "\n\n---\n\n".join(contents)

        # --- Agent call ---
        try:
            def _deriv_fn():
                return self._agent.derive(combined_content)

            deriv = call_with_retry(_deriv_fn)
        except Exception as e:
            return {
                "status": "error",
                "detail": str(e),
                "parent_ids": list(parent_ids),
            }

        deriv_id = str(uuid.uuid4())

        # --- Adversarial validation gate ---
        if self._validator is not None:
            passes, warning = validate_derivation(
                self._validator, combined_content, deriv
            )
            if warning:
                import json as _json
                import sys as _sys

                _sys.stderr.write(
                    _json.dumps({"validator_warning": warning}) + "\n"
                )

            if not passes:
                return {
                    "status": "quality_failed",
                    "reason": "Synthesis does not meaningfully re-elaborate the source material.",
                    "parent_ids": list(parent_ids),
                }

        # --- Write markdown file ---
        self._vault_path.mkdir(parents=True, exist_ok=True)
        first_line = deriv.prose.split("\n")[0].strip()
        head_name = (
            first_line.lstrip("# ").strip().strip('"').strip("'") or deriv_id
        )
        md_path = self._human_path(head_name)
        md_path.write_text(deriv.prose, encoding="utf-8")

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
            )

        # Synthesis: confidence = min(parents' confidence)
        confidences: list[str] = []
        for pid in parent_ids:
            p = self._store.get_node(pid)
            if p and p.get("confidence"):
                confidences.append(p["confidence"])
        if "low" in confidences:
            synth_conf = "low"
        elif "medium" in confidences:
            synth_conf = "medium"
        else:
            synth_conf = "low"
        self._store._con.execute(
            "UPDATE node SET confidence = ? WHERE id = ?",
            (synth_conf, deriv_id),
        )

        # --- Content checks ---
        from memex.checks import run_checks

        check_result = run_checks(self._store._con, deriv_id, md_path)
        trust_state = "auto-verified" if check_result.passed else "draft"
        self._store.update_trust_state(
            node_id=deriv_id,
            trust_state=trust_state,
            check_failures=check_result.failures,
        )

        return {
            "id": deriv_id,
            "status": "synthesized",
            "parent_ids": list(parent_ids),
            "trust_state": trust_state,
            "content_path": str(md_path),
            "check_failures": check_result.failures,
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
