"""Fake LLMClient that produces a derivation that FAILS checks.

Specifically: no "> Synthesis:" marker, and content is too short to pass size check.
Used to test that the checks module correctly leaves the node in draft state.
"""
from __future__ import annotations

from memex.llm_client import DerivationResult


class FakeLLMClientFailing:
    """Fake LLM client that returns a derivation missing the synthesis marker."""

    def derive(self, content: str) -> DerivationResult:
        # Deliberately omit "> Synthesis:" and keep it short (< MIN_CHARS=100)
        prose = "This derivation has no synthesis marker and is intentionally bad."
        return DerivationResult(prose=prose, synthesis_statements=[])
