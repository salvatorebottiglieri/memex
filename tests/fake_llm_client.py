"""Fake Agent for tests — no real Anthropic calls.

Returns predictable derivation prose that includes at least one > Synthesis: marker.
Provides a deterministic review() returning configurable ReviewProposal.
"""
from __future__ import annotations

from memex.agent import Agent
from memex.schemas import DerivationResult, DocumentRef, ReviewProposal

class FakeAgent(Agent):
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

    def extract_ideas(self, content: str, source_url: str | None = None) -> list[str]:
        return ["Key idea 1", "Key idea 2", "Key idea 3"]

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

class FakeAgentValidRefs(Agent):
    """Fake agent returning realistic referencable values.

    Unlike FakeAgent (which returns fake node IDs like 'n1','n2'),
    this agent returns damage_boundary_node_id=None to satisfy the FK constraint.
    """

    def derive(self, content: str) -> dict:
        return {"prose": "fake", "synthesis_statements": []}


    def extract_ideas(self, content: str, source_url: str | None = None) -> list[str]:
        return ["Idea 1", "Idea 2"]

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> dict:
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Fake review: all good.",
            confidence="high",
        )

class FakeAgentThrowsOnReview(Agent):
    """Fake agent that raises on every review() call.

    Used to test per-event error recovery in the review batch command.
    """

    def derive(self, content: str) -> dict:
        return {"prose": "fake", "synthesis_statements": []}

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        raise RuntimeError("Simulated LLM review failure")



    def extract_ideas(self, content: str, source_url: str | None = None) -> list[str]:
        pass


class FakeReaderAgent(Agent):
    """Reader-capable fake: records what the service hands it (content vs reference)."""

    can_read_files = True

    def __init__(self):
        self.received: dict = {}

    def derive(self, content=None, *, reference=None):
        self.received = {"content": content, "reference": reference}
        return DerivationResult(
            prose="# Notes\n\nBody.\n\n> Synthesis: A reader-mode claim.\n",
            synthesis_statements=["A reader-mode claim."],
        )

    def review(self, target_content, asserting_content, edge_payload):
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Reader fake review.",
            confidence="high",
        )

    def extract_ideas(self, content=None, source_url=None, *, reference=None):
        return ["Reader idea"]


class FakeAgentDivergent(Agent):
    """Fake whose prose markers deliberately diverge from synthesis_statements.

    Ticket #143: the service canonicalizes the file's ``> Synthesis:`` markers
    from the column, so D3's exact file-vs-column check must pass even when
    the agent renders the same statements with different quoting/style.
    Construct with explicit ``prose``/``statements`` to shape each scenario;
    the defaults reproduce the divergent-quoting case (single quotes in prose,
    double quotes in the column).
    """

    def __init__(
        self,
        prose: str | None = None,
        statements: list[str] | None = None,
    ):
        self._prose = prose
        self._statements = statements

    def derive(
        self,
        content: str | None = None,
        *,
        reference: DocumentRef | None = None,
    ) -> DerivationResult:
        prose = self._prose if self._prose is not None else (
            "# The Claim\n\n"
            "This article discusses the topic at hand and its broader implications.\n\n"
            "> Synthesis: The 'x' claim\n\n"
            "The source material covers the subject thoroughly."
        )
        statements = self._statements if self._statements is not None else [
            'The "x" claim'
        ]
        return DerivationResult(prose=prose, synthesis_statements=statements)

    def review(
        self, target_content: str, asserting_content: str, edge_payload: dict
    ) -> ReviewProposal:
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Fake review: all good.",
            confidence="high",
        )

    def extract_ideas(
        self,
        content: str | None = None,
        source_url: str | None = None,
        *,
        reference: DocumentRef | None = None,
    ) -> list[str]:
        return ["Key idea 1", "Key idea 2", "Key idea 3"]
