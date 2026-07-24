"""Fake Agent that always raises from derive().

Used to test that _derive_all correctly catches persistent agent errors
and reports them as status="error" without crashing the batch.
"""
from __future__ import annotations


class FakeLLMClientThrows:
    """Fake LLM client that raises an exception on every derive() call."""

    def derive(self, content: str) -> None:
        raise RuntimeError("Simulated LLM failure")

    def extract_ideas(self, content: str, source_url: str | None = None) -> list[str]:
        raise RuntimeError("Simulated LLM failure")

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        raise NotImplementedError(
            "FakeLLMClientThrows is for derive-failure testing only; "
            "review() should not be called in these tests."
        )
