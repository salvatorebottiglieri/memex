"""Fake Validator that always fails the quality gate.

Has call_llm returning {"passes": false} so validate_derivation rejects.
"""
from __future__ import annotations

from memex.agent import DerivationResult


class FakeValidatorFails:
    """Fake validator that always returns quality_failed."""

    def derive(self, content: str) -> DerivationResult:
        return DerivationResult(
            prose="Validator derivation stub.",
            synthesis_statements=["Stub statement."],
        )

    def call_llm(self, prompt: str) -> str:
        return '{"passes": false}'

    def extract_ideas(self, content: str) -> list[str]:
        return ["stub idea"]

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        pass
