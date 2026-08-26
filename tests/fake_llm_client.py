"""Fake Agent for tests — no real Anthropic calls.

Returns predictable derivation prose that includes at least one > Synthesis: marker.
Provides a deterministic review() returning configurable ReviewProposal.
"""
from __future__ import annotations

import json
import re

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


class FakeJudge(Agent):
    """Deterministic fake judge for the always-on V1/V2 validations.

    ``call_llm`` inspects the validation prompt and returns structured
    verdicts mirroring the V1/V2 prompt contracts:

      - V1: a claim containing ``unsupported`` (default
        'SENTINEL-UNSUPPORTED') → UNSUPPORTED; in a synthesis (context line
        'Node tier: synthesis') a claim with no inline link → UNSUPPORTED
        (missing declaration); otherwise SUPPORTED. SUPPORTED verdicts cite
        a literal excerpt of the cited source's content (parsed from the
        prompt's parent block), UNSUPPORTED verdicts carry
        source_examined + absence_explanation. ``fabricate`` (default False)
        makes SUPPORTED verdicts cite a quote that is NOT in the source —
        exercising D7.
      - V2: passes=True unless a synthesis statement contains ``boilerplate``
        (default 'SENTINEL-BOILERPLATE').
    """

    def __init__(
        self,
        unsupported: str = "SENTINEL-UNSUPPORTED",
        boilerplate: str = "SENTINEL-BOILERPLATE",
        fabricate: bool = False,
    ):
        self.unsupported = unsupported
        self.boilerplate = boilerplate
        self.fabricate = fabricate

    # -- prompt parsing -------------------------------------------------

    @staticmethod
    def _parse_parents(prompt: str) -> dict[str, str]:
        parents = {}
        for m in re.finditer(
            r"Parent \d+: (\S+) \(node [^)]*\)\n(.*?)(?=\n\nParent \d+: |\Z)",
            prompt,
            re.S,
        ):
            parents[m.group(1)] = m.group(2)
        return parents

    @staticmethod
    def _cited_source(claim: str, parents: dict[str, str]) -> str | None:
        links = re.findall(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]", claim)
        if links:
            return links[0][0]
        return next(iter(parents), None)  # notes: the single parent

    @staticmethod
    def _excerpt(filename: str | None, parents: dict[str, str]) -> str:
        content = re.sub(r"\s+", " ", parents.get(filename or "", "")).strip()
        return content[:80]

    @staticmethod
    def _presented_claims(prompt: str) -> list[str]:
        """The V1 claims as presented in the prompt's slice section.

        Quote-tolerant extraction, mirroring the production
        ``_slice_claim_text`` contract: a claim runs to its TRAILING
        delimiter — the last double quote on its ``Claim N: "..."`` line
        (the rendered block is ``Claim N: "<claim>"``, with V1's optional
        ``\n  links: …`` continuation line) — so embedded double quotes
        inside the claim text are part of the claim, never the end of it.
        A judge that read the prompt echoes the FULL claim text.
        """
        claims = []
        for line in prompt.splitlines():
            m = re.match(r'^Claim \d+: "(.*)"$', line)
            if m:
                claims.append(m.group(1))
        return claims

    # -- verdict generation ---------------------------------------------

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            statements_section = prompt.split("Synthesis statements:", 1)[1].split(
                "Node body:", 1
            )[0]
            passes = self.boilerplate not in statements_section
            return json.dumps(
                {"passes": passes, "reason": "" if passes else "statement is boilerplate"}
            )
        parents = self._parse_parents(prompt)
        claims = self._presented_claims(prompt)
        is_synthesis = "Node tier: synthesis" in prompt
        verdicts = []
        for claim in claims:
            source = self._cited_source(claim, parents)
            if self.unsupported in claim or (is_synthesis and "[[" not in claim):
                verdicts.append(
                    {
                        "claim": claim,
                        "verdict": "UNSUPPORTED",
                        "source_examined": source or "no linked parent",
                        "absence_explanation": (
                            "missing declaration"
                            if is_synthesis and "[[" not in claim
                            else "source content does not contain the claim"
                        ),
                    }
                )
            else:
                verdicts.append(
                    {
                        "claim": claim,
                        "verdict": "SUPPORTED",
                        "evidence_quote": (
                            "this fabricated quote appears nowhere in the source"
                            if self.fabricate
                            else self._excerpt(source or "", parents)
                        ),
                    }
                )
        return json.dumps({"verdicts": verdicts})

    def derive(
        self,
        content: str | None = None,
        *,
        reference: DocumentRef | None = None,
    ) -> DerivationResult:
        return DerivationResult(
            prose="# Judge stub\n\nJudge derivation stub.\n\n> Synthesis: stub\n",
            synthesis_statements=["stub"],
        )

    def review(
        self, target_content: str, asserting_content: str, edge_payload: dict
    ) -> ReviewProposal:
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Fake judge review.",
            confidence="high",
        )

    def extract_ideas(
        self,
        content: str | None = None,
        source_url: str | None = None,
        *,
        reference: DocumentRef | None = None,
    ) -> list[str]:
        return ["Judge idea"]


class FakeJudgeNotesHonest(FakeJudge):
    """Fake judge that honestly grounds notes on the single parent even when
    the note prose carries stray wikilinks (D6's notes-tier exemption).

    ``_cited_source`` always resolves to the single parent instead of
    following the claim's link filenames — mirroring the V1 prompt's notes
    rule ("A claim with no link in a notes derivation is judged against its
    single parent"): a stray [[non-parent|...]] link does not change what a
    note is grounded against. Only the notes path is exercised (the class
    keeps FakeJudge's V2 and UNSUPPORTED behavior).
    """

    @staticmethod
    def _cited_source(claim: str, parents: dict[str, str]) -> str | None:
        return next(iter(parents), None)


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
