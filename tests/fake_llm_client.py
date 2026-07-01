"""Fake LLMClient for tests — no real Anthropic calls.

Returns predictable derivation prose that includes at least one > Synthesis: marker.
"""
from __future__ import annotations

from memex.llm_client import DerivationResult


class FakeLLMClient:
    """Deterministic LLM client for tests."""

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
