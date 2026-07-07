"""Fake Agent for tests — no real Anthropic calls.

Returns predictable derivation prose that includes at least one > Synthesis: marker.
Provides a deterministic review() returning configurable ReviewProposal.
"""
from __future__ import annotations

from memex.agent import DerivationResult, ReviewProposal

class FakeAgent:
    """Deterministic Agent for tests."""

    def __init__(
        self,
        review_affected_node_ids: list[str] | None = None,
        review_confidence: str = "high",
    ):
        self.review_affected_node_ids = review_affected_node_ids
        self.review_confidence = review_confidence

    def derive(self, content: str) -> DerivationResult:
        prose = (
            "This article discusses the topic at hand.\n\n"
            "> Synthesis: The author implies a broader pattern beyond what is stated directly.\n\n"
            "The source material covers the subject thoroughly."
        )
        synthesis_statements = [
            "The author implies a broader pattern beyond what is stated directly."
        ]
        return DerivationResult(prose=prose, synthesis_statements=synthesis_statements)

    def generate_title(self, content: str, url: str) -> str | None:
        return None

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> ReviewProposal:
        rp_affected = self.review_affected_node_ids
        if rp_affected is None:
            rp_affected = ["n1", "n2"]
        damage_boundary = rp_affected[-1] if rp_affected else None
        return ReviewProposal(
            affected_node_ids=list(rp_affected),
            damage_boundary_node_id=damage_boundary,
            rationale_md="Fake review: the contested claim affects downstream nodes.",
            confidence=self.review_confidence,
        )
