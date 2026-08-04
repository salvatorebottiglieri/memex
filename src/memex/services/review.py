"""ReviewService — orchestration for review operations.

Encapsulates: loading pending contestation events, loading target and
asserting node content, running agent review, and writing review
proposals.
"""

from __future__ import annotations

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

    @staticmethod
    def _node_review_packet(node: dict) -> str:
        """Compact review reference for a node — metadata plus the node's
        synthesis statements (the contested claims). Never the full content."""
        lines = [
            f"- id: {node.get('id')}",
            f"- title: {node.get('title') or '(no title)'}",
            f"- source_url: {node.get('source_url') or '(none)'}",
            f"- kind: {node.get('kind')}",
            f"- tier: {node.get('tier') or '(none)'}",
        ]
        statements = node.get("synthesis_statements") or []
        if statements:
            claims = "\n".join(f"  - {s}" for s in statements)
            lines.append(f"- synthesis statements:\n{claims}")
        else:
            lines.append(
                "- synthesis statements: (none — content-bearing L0, no claims extracted)"
            )
        return "\n".join(lines)

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
        if target_node is None:
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

        asserting_node = self._store.get_node(edge_rows["from_node"])
        if asserting_node is None:
            return {
                "event_id": event["id"],
                "status": "error",
                "detail": "asserting_node_not_found",
            }

        # Token-frugal review: pass compact references (metadata + synthesis
        # statements — the contested claims), never the full content files.
        target_packet = self._node_review_packet(target_node)
        asserting_packet = self._node_review_packet(asserting_node)
        edge_payload = {"edge_id": event["edge_id"]}

        def _review_fn():
            return self._agent.review(
                target_packet, asserting_packet, edge_payload
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
