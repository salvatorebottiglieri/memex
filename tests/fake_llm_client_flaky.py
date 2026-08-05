"""Fake Agent that fails the first N derive() calls, then succeeds.

Used to test the retry path in `derive --all` (call_with_retry with
use_retry=True). Fresh interpreter per CLI subprocess, so the failure
counter always starts at zero.
"""
from __future__ import annotations

from memex.agent import Agent
from memex.schemas import DerivationResult


class FakeLLMClientFlaky(Agent):
    """Fails the first two derive() calls, succeeds on the third."""

    def __init__(self) -> None:
        self._calls = 0

    def derive(self, content: str) -> DerivationResult:
        self._calls += 1
        if self._calls <= 2:
            raise RuntimeError(f"transient failure {self._calls}")
        prose = (
            "This derivation is long enough to pass the size check and it "
            "carries the required synthesis marker.\n\n"
            "> Synthesis: A broader pattern emerges from the source material."
        )
        return DerivationResult(
            prose=prose,
            synthesis_statements=["A broader pattern emerges from the source material."],
        )

    def extract_ideas(self, content: str, source_url: str | None = None) -> list[str]:
        return ["Idea 1"]

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        raise NotImplementedError("FakeLLMClientFlaky is for derive-retry testing only.")
