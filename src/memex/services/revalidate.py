"""RevalidatorService — re-run the current gate on an existing node.

The validation gate (ADR-0018) runs only at creation, so a node keeps the
verdict of the gate as it was then. This service re-judges an existing node
with today's gate over the *same* inputs ``DeriverService`` uses (the node's
own content file plus its provenance parents' files) and writes back only the
state columns — ``trust_state``, ``check_failures`` and ``evidence``. The
markdown file is never touched: if the prose is wrong, the state says so, and
re-deriving stays a separate, conscious decision (issue #171).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from memex.agent import Agent
from memex.checks import run_checks
from memex.store import Store
from memex.validators.validate import (
    merge_gate_failures,
    run_validations,
    validation_environment,
)


@dataclass
class RevalidateResult:
    """One node's re-judgement: the state before and after, never the prose."""

    id: str
    # "revalidated" | "error"
    status: str
    previous_trust_state: str | None = None
    trust_state: str | None = None
    check_failures: list[str] | None = None
    # A trusted node (anything not already draft) fell to draft.
    demoted: bool = False
    detail: str | None = None


class RevalidatorService:
    """Re-judge existing nodes with the current gate, without rewriting them.

    No vault path: nothing is written to disk. Dependencies are the store and
    the agent, exactly like ``DeriverService``/``SynthesizerService``.
    """

    def __init__(self, store: Store, agent: Agent) -> None:
        self._store = store
        # Judge + enabled flag: single source of truth shared with
        # derive/synthesize (validators.validate.validation_environment).
        self._judge, self._validations_enabled = validation_environment(agent)

    def revalidate(self, node_id: str) -> RevalidateResult:
        """Re-run D1–D6 + the V1 → D7 → V2 DAG on an existing node.

        Returns a result; agent/parse failures degrade exactly as in derive
        (warnings, never a crash). Never raises for a missing node or file —
        those come back as ``status="error"``.
        """
        node = self._store.get_node(node_id)
        if node is None:
            return RevalidateResult(
                id=node_id, status="error", detail="node_not_found"
            )
        content_path = node.get("content_path")
        if not content_path or not Path(content_path).exists():
            return RevalidateResult(
                id=node_id, status="error", detail="content_not_found"
            )

        check_result = run_checks(self._store._con, node_id, content_path)
        validation_result = None
        if self._validations_enabled:
            validation_result = run_validations(
                self._judge, self._store._con, node_id, content_path
            )

        failures, trust_state = merge_gate_failures(
            check_result, validation_result
        )
        self._store.update_trust_state(
            node_id=node_id, trust_state=trust_state, check_failures=failures
        )
        if validation_result is not None:
            self._store.update_evidence(node_id, validation_result.evidence)

        previous = node.get("trust_state")
        return RevalidateResult(
            id=node_id,
            status="revalidated",
            previous_trust_state=previous,
            trust_state=trust_state,
            check_failures=failures,
            demoted=previous not in (None, "draft") and trust_state == "draft",
        )

    def revalidate_all(self, limit: int | None = None) -> list[RevalidateResult]:
        """Re-judge every summary node, capped at *limit* when given.

        ``kind='summary'`` is exactly the derive/synthesize output — the only
        nodes that carry a gate verdict (url/extracted/raw_source roots have
        no provenance parents to judge against). Frozen nodes are excluded by
        ``list_nodes`` (ADR-0019). ``limit=None`` or ``limit <= 0`` means
        unlimited.
        """
        nodes = self._store.list_nodes(kind="summary")
        if limit is not None and limit > 0:
            nodes = nodes[:limit]
        return [self.revalidate(node["id"]) for node in nodes]
