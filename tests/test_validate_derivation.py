"""Tests for validate_derivation() — adversarial validation gate.

Covers all dispatch paths:
  - DemoAgent → (True, None)
  - Agent with call_llm returning {"passes": true} → (True, None)
  - Agent with call_llm returning {"passes": false} → (False, None)
  - Agent with call_llm raising exception → (True, "warning message")
  - Agent with call_llm returning non-JSON → (True, "warning message")
  - Agent with call_llm returning valid JSON but missing "passes" → (True, "warning message")
  - Unknown agent (no call_llm) → (True, None)
"""
from __future__ import annotations

import json

from memex.derivers.demo import DemoAgent
from memex.schemas import DerivationResult
from memex.validators.validate import validate_derivation


class _CallLlmAgent:
    """Agent with a controlled call_llm method."""

    def __init__(self, call_llm_fn):
        self._call_llm_fn = call_llm_fn

    def call_llm(self, prompt: str) -> str:
        return self._call_llm_fn(prompt)


class _NoCallLlmAgent:
    """Agent without a call_llm method — simulates unknown agent type."""
    pass


# ── Shared fixture data ──────────────────────────────────────────

_PARENT_CONTENT = "Some source material about machine learning."
_DERIV = DerivationResult(
    prose="This article explains key ML concepts like supervised learning.",
    synthesis_statements=["The author implies ML is broadly applicable."],
)


# ── Tests ───────────────────────────────────────────────────────

class TestValidateDerivation:
    """validate_derivation dispatch table."""

    def test_demo_agent_passes(self):
        """DemoAgent always passes with no warning."""
        result = validate_derivation(DemoAgent(), _PARENT_CONTENT, _DERIV)
        assert result == (True, None), f"Expected (True, None), got {result}"

    def test_call_llm_returns_true(self):
        """Agent returning {"passes": true} → (True, None)."""
        agent = _CallLlmAgent(lambda _: json.dumps({"passes": True}))
        result = validate_derivation(agent, _PARENT_CONTENT, _DERIV)
        assert result == (True, None), f"Expected (True, None), got {result}"

    def test_call_llm_returns_false(self):
        """Agent returning {"passes": false} → (False, None)."""
        agent = _CallLlmAgent(lambda _: json.dumps({"passes": False}))
        result = validate_derivation(agent, _PARENT_CONTENT, _DERIV)
        assert result == (False, None), f"Expected (False, None), got {result}"

    def test_call_llm_raises_exception(self):
        """Agent whose call_llm raises → (True, warning)."""
        def _raise(_):
            raise RuntimeError("LLM unavailable")

        agent = _CallLlmAgent(_raise)
        result = validate_derivation(agent, _PARENT_CONTENT, _DERIV)
        assert result[0] is True, f"Expected passes=True, got passes={result[0]}"
        assert isinstance(result[1], str), f"Expected warning string, got {result[1]}"
        assert "Validator LLM call failed" in result[1]

    def test_call_llm_returns_garbage(self):
        """Agent returning non-JSON → (True, warning)."""
        agent = _CallLlmAgent(lambda _: "not json at all")
        result = validate_derivation(agent, _PARENT_CONTENT, _DERIV)
        assert result[0] is True, f"Expected passes=True, got passes={result[0]}"
        assert isinstance(result[1], str), f"Expected warning string, got {result[1]}"
        assert "Validator response parse failed" in result[1]

    def test_call_llm_returns_valid_json_missing_passes(self):
        """Agent returning valid JSON but no 'passes' key → (True, warning)."""
        agent = _CallLlmAgent(lambda _: json.dumps({"something": "else"}))
        result = validate_derivation(agent, _PARENT_CONTENT, _DERIV)
        assert result[0] is True, f"Expected passes=True, got passes={result[0]}"
        assert isinstance(result[1], str), f"Expected warning string, got {result[1]}"
        assert "missing 'passes' field" in result[1]

    def test_unknown_agent_no_call_llm(self):
        """Agent without call_llm method → (True, None)."""
        agent = _NoCallLlmAgent()
        result = validate_derivation(agent, _PARENT_CONTENT, _DERIV)
        assert result == (True, None), f"Expected (True, None), got {result}"


class TestValidateReaderMode:
    """Reader validators get a reference block and read-enabled calls."""

    def test_reader_validator_receives_reference_block(self):
        from memex.derivers.pi import OMPRpcAgent
        from memex.schemas import DerivationResult, DocumentRef
        from memex.validators.validate import validate_derivation

        captured = {}

        class ReaderValidator(OMPRpcAgent):
            def call_llm(self, prompt, *, allow_read=False):
                captured["allow_read"] = allow_read
                captured["has_path"] = "/tmp/parent.md" in prompt
                return '{"passes": true}'

        v = ReaderValidator()
        ref = DocumentRef(node_id="p1", content_path="/tmp/parent.md", size_bytes=42)
        ok, warn = validate_derivation(
            v, None, DerivationResult(prose="# X", synthesis_statements=["s"]),
            reference=ref,
        )
        assert ok is True and warn is None
        assert captured["allow_read"] is True
        assert captured["has_path"] is True
