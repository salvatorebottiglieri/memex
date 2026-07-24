"""Built-in demo deriver — returns hardcoded content. No API key needed."""

from memex.schemas import DerivationResult, ReviewProposal
from memex.agent import Agent


class DemoAgent(Agent):
    """Built-in demo agent — returns hardcoded content. No API key needed."""

    def derive(self, content: str) -> DerivationResult:
        prose = (
            "This article discusses the topic at hand.\n\n"
            "> Synthesis: The author implies a broader pattern beyond what is stated directly.\n\n"
            "The source material covers the subject thoroughly."
        )
        return DerivationResult(
            prose=prose,
            synthesis_statements=[
                "The author implies a broader pattern beyond what is stated directly."
            ],
        )

    def review(
        self, target_content: str, asserting_content: str, edge_payload: dict
    ) -> ReviewProposal:
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Demo review: no contestation analysis performed.",
            confidence="high",
        )

    def generate_title(self, content: str, url: str) -> str | None:
        """Demo: no title generation."""
        return None

    def extract_ideas(self, content: str) -> list[str]:
        """Demo: return 3 hardcoded ideas."""
        return [
            "Key idea one from the source material",
            "Key idea two from the source material",
            "Key idea three from the source material",
        ]
