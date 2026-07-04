"""Fake Agent that produces a derivation that FAILS checks.

Specifically: no "> Synthesis:" marker, and content is too short to pass size check.
Used to test that the checks module correctly leaves the node in draft state.
"""
from __future__ import annotations

from memex.agent import DerivationResult


class FakeLLMClientFailing:
    """Fake LLM client that returns a derivation missing the synthesis marker."""

    def derive(self, content: str) -> DerivationResult:
        # Deliberately omit "> Synthesis:" and keep it short (< MIN_CHARS=100)
        prose = "This derivation has no synthesis marker and is intentionally bad."
        return DerivationResult(prose=prose, synthesis_statements=[])

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        raise NotImplementedError(
            "FakeLLMClientFailing is for derive-failure testing only; "
            "review() should not be called in these tests."
        )
