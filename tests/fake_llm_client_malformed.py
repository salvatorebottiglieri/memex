"""Fake Agent that returns an out-of-schema derive() response.

Simulates a model emitting a plain dict instead of a DerivationResult —
the service must treat it as a hard failure, not a partial state.
"""
from __future__ import annotations

from memex.agent import Agent


class FakeLLMClientMalformed(Agent):
    """derive() returns a plain dict — not a DerivationResult."""

    def derive(self, content: str) -> dict:
        return {"prose": "not a real derivation", "synthesis_statements": []}

    def extract_ideas(self, content: str, source_url: str | None = None) -> list[str]:
        return ["Idea 1"]

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        raise NotImplementedError("FakeLLMClientMalformed is for derive-failure testing only.")
