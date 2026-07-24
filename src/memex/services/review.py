"""ReviewService — orchestration for review operations.

Encapsulates: loading pending contestation events, loading target and
asserting node content, running agent review, and writing review
proposals.
"""

from __future__ import annotations

from pathlib import Path

from memex.agent import Agent
from memex.store import Store
from memex.utils.retry import call_with_retry


class ReviewService:
    """Orchestrate review operations behind a small interface.

    Callers provide dependencies via constructor, then call
    ``review_batch()``.
    """

    def __init__(self, store: Store, agent: Agent) -> None:
        self._store = store
        self._agent = agent

    def review_batch(self) -> list[dict]:
        """Process all pending contestation events without proposals.

        For each event, loads the target and asserting node content,
        calls ``agent.review()``, and writes a review proposal.

        Returns a list of result dicts (never raises — individual
        failures are captured in the results).
        """
        events = self._store.get_pending_events_without_proposal()
        results: list[dict] = []

        for event in events:
            try:
                result = self._process_event(event)
                results.append(result)
            except Exception as e:
                results.append(
                    {
                        "event_id": event["id"],
                        "status": "error",
                        "detail": str(e),
                    }
                )

        return results

    def _process_event(self, event: dict) -> dict:
        """Process a single contestation event into a review proposal."""
        target_node = self._store.get_node(event["target_node_id"])
        if target_node is None or not target_node.get("content_path"):
            return {
                "event_id": event["id"],
                "status": "error",
                "detail": "target_node_not_found",
            }

        # Find the asserting node (from_node of the contradicts edge)
        edge_rows = self._store._con.execute(
            "SELECT from_node FROM edge WHERE id = ?",
            (event["edge_id"],),
        ).fetchone()
        if edge_rows is None:
            return {
                "event_id": event["id"],
                "status": "error",
                "detail": "edge_not_found",
            }

        asserting_node_id = edge_rows["from_node"]
        asserting_node = self._store.get_node(asserting_node_id)
        if asserting_node is None or not asserting_node.get("content_path"):
            return {
                "event_id": event["id"],
                "status": "error",
                "detail": "asserting_node_not_found",
            }

        target_content = Path(target_node["content_path"]).read_text(
            encoding="utf-8"
        )
        asserting_content = Path(
            asserting_node["content_path"]
        ).read_text(encoding="utf-8")
        edge_payload = {"edge_id": event["edge_id"]}

        def _review_fn():
            return self._agent.review(
                target_content, asserting_content, edge_payload
            )

        proposal = call_with_retry(_review_fn)

        proposal_id = self._store.write_review_proposal(
            event_id=event["id"],
            affected_node_ids=proposal.affected_node_ids,
            damage_boundary_node_id=proposal.damage_boundary_node_id,
            rationale_md=proposal.rationale_md,
            confidence=proposal.confidence,
        )

        return {
            "event_id": event["id"],
            "proposal_id": proposal_id,
            "status": "proposed",
        }
