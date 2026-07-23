"""Fake Validator that warns during validation.

Has call_llm that raises, so validate_derivation returns (True, warning).
"""
from __future__ import annotations

from memex.agent import DerivationResult


class FakeValidatorWarns:
    """Fake validator that warns (call_llm raises)."""

    def derive(self, content: str) -> DerivationResult:
        return DerivationResult(
            prose="Validator derivation stub.",
            synthesis_statements=["Stub statement."],
        )

    def call_llm(self, prompt: str) -> str:
        raise RuntimeError("Simulated validator LLM failure")

    def extract_ideas(self, content: str) -> list[str]:
        return ["stub idea"]

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        pass
