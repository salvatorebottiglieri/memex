"""Tests for Agent — ReviewProposal, review() method, and MEMEX_AGENT loading.

Direct import tests, no subprocess, no DB. Uses FakeAgent.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memex.agent import (
    Agent,
    ReviewProposal,
    load_agent,
)

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"


class TestReviewProposal:
    """ReviewProposal dataclass contract."""

    def test_review_proposal_fields(self):
        """All four fields are present and typed correctly."""
        rp = ReviewProposal(
            affected_node_ids=["n1", "n2"],
            damage_boundary_node_id="n2",
            rationale_md="Because n1 relies on the claim.",
            confidence="high",
        )
        assert rp.affected_node_ids == ["n1", "n2"]
        assert rp.damage_boundary_node_id == "n2"
        assert rp.rationale_md == "Because n1 relies on the claim."
        assert rp.confidence == "high"

    def test_review_proposal_nullable_boundary(self):
        """damage_boundary_node_id is nullable."""
        rp = ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="No damage.",
            confidence="low",
        )
        assert rp.damage_boundary_node_id is None


class TestAgentBase:
    """Agent base class contract."""

    def test_review_raises_not_implemented(self):
        """Agent.review raises NotImplementedError by default."""
        client = Agent()
        with pytest.raises(NotImplementedError):
            client.review("target", "asserting", {})

    def test_derive_still_raises_not_implemented(self):
        """Agent.derive still raises NotImplementedError (no regression)."""
        client = Agent()
        with pytest.raises(NotImplementedError):
            client.derive("content")


class TestFakeAgent:
    """FakeAgent.review returns deterministic ReviewProposal."""

    def test_review_with_custom_args(self):
        """Constructor args shape the returned ReviewProposal."""
        client = load_agent(FAKE_AGENT)
        client.review_affected_node_ids = ["n3", "n4", "n5"]
        client.review_confidence = "medium"

        rp = client.review("target", "asserting", {})

        assert isinstance(rp, ReviewProposal)
        assert rp.affected_node_ids == ["n3", "n4", "n5"]
        assert rp.damage_boundary_node_id == "n5"
        assert rp.confidence == "medium"
        assert isinstance(rp.rationale_md, str)
        assert len(rp.rationale_md) > 0

    def test_review_with_default_args(self):
        """Default args produce a predictable ReviewProposal."""
        client = load_agent(FAKE_AGENT)

        rp = client.review("target", "asserting", {})

        assert isinstance(rp, ReviewProposal)
        assert rp.affected_node_ids == ["n1", "n2"]
        assert rp.damage_boundary_node_id == "n2"
        assert rp.confidence == "high"
        assert isinstance(rp.rationale_md, str)

    def test_review_empty_affected_ids(self):
        """When affected_node_ids is empty, damage_boundary_node_id is None."""
        client = load_agent(FAKE_AGENT)
        client.review_affected_node_ids = []

        rp = client.review("target", "asserting", {})

        assert rp.affected_node_ids == []
        assert rp.damage_boundary_node_id is None
        assert rp.confidence == "high"

    def test_review_returns_review_proposal(self):
        """Verify that review returns the correct type."""
        client = load_agent(FAKE_AGENT)
        rp = client.review("target", "asserting", {})
        assert isinstance(rp, ReviewProposal)

    def test_derive_still_works(self):
        """FakeAgent.derive is unaffected by the new review method."""
        client = load_agent(FAKE_AGENT)
        dr = client.derive("Some content here.")
        assert dr.prose is not None
        assert len(dr.prose) > 0


class TestLoadAgent:
    """MEMEX_AGENT loading works for both derive and review."""

    def test_fake_llm_derive_and_review(self):
        """FakeAgent loaded via MEMEX_AGENT satisfies both methods."""
        client = load_agent(FAKE_AGENT)
        # derive
        dr = client.derive("content")
        assert dr.prose is not None
        assert "Synthesis:" in dr.prose
        # review
        rp = client.review("target", "asserting", {})
        assert isinstance(rp, ReviewProposal)
        assert rp.confidence == "high"

    def test_default_agent_derive_and_review(self):
        """Default (no module_path) loads DemoAgent with both methods."""
        client = load_agent()
        # Both methods should exist (review raises NotImplementedError only on base)
        assert hasattr(client, "derive")
        assert hasattr(client, "review")


class TestAnthropicAgent:
    """AnthropicAgent.review — method signature and JSON parsing.

    Injects a fake `anthropic` module into sys.modules to avoid installing the real SDK.
    """

    @staticmethod
    def _make_mock_anthropic(message_text: str):
        """Build a fake anthropic module returning the given message text."""
        import sys  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        fake_message = MagicMock()
        fake_message.content = [MagicMock(text=message_text)]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_message

        fake_module = MagicMock()
        fake_module.Anthropic.return_value = mock_client

        return fake_module

    def test_review_exists(self):
        """AnthropicAgent has a review method."""
        from memex.agent import AnthropicAgent  # noqa: PLC0415

        client = AnthropicAgent()
        assert hasattr(client, "review")

    def test_review_returns_review_proposal(self):
        """AnthropicAgent.review returns a ReviewProposal from valid JSON."""
        import json  # noqa: PLC0415
        import sys  # noqa: PLC0415

        from memex.agent import AnthropicAgent  # noqa: PLC0415

        fake_json = json.dumps({
            "affected_node_ids": ["n1", "n2"],
            "damage_boundary_node_id": "n2",
            "rationale_md": "Because the claim is central.",
            "confidence": "high",
        })

        fake_module = self._make_mock_anthropic(fake_json)

        saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = fake_module
        try:
            client = AnthropicAgent()
            rp = client.review("target content", "asserting content", {"claim": "x"})
        finally:
            if saved is None:
                del sys.modules["anthropic"]
            else:
                sys.modules["anthropic"] = saved

        assert isinstance(rp, ReviewProposal)
        assert rp.affected_node_ids == ["n1", "n2"]
        assert rp.damage_boundary_node_id == "n2"
        assert rp.confidence == "high"
        assert "central" in rp.rationale_md

    def test_review_malformed_json(self):
        """Malformed JSON degrades to safe defaults (no exception)."""
        import sys  # noqa: PLC0415

        from memex.agent import AnthropicAgent  # noqa: PLC0415

        fake_module = self._make_mock_anthropic("{Not valid JSON]")

        saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = fake_module
        try:
            client = AnthropicAgent()
            rp = client.review("target", "asserting", {})
        finally:
            if saved is None:
                del sys.modules["anthropic"]
            else:
                sys.modules["anthropic"] = saved

        assert isinstance(rp, ReviewProposal)
        assert rp.affected_node_ids == []
        assert rp.damage_boundary_node_id is None
        assert rp.confidence == "low"
        assert "{Not valid JSON]" in rp.rationale_md

    def test_review_partial_json(self):
        """Partial JSON with missing fields fills from defaults."""
        import json  # noqa: PLC0415
        import sys  # noqa: PLC0415

        from memex.agent import AnthropicAgent  # noqa: PLC0415

        # Missing damage_boundary_node_id and confidence
        partial = json.dumps({
            "affected_node_ids": ["n1"],
            "rationale_md": "Partial response.",
        })

        fake_module = self._make_mock_anthropic(partial)

        saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = fake_module
        try:
            client = AnthropicAgent()
            rp = client.review("target", "asserting", {})
        finally:
            if saved is None:
                del sys.modules["anthropic"]
            else:
                sys.modules["anthropic"] = saved

        assert isinstance(rp, ReviewProposal)
        assert rp.affected_node_ids == ["n1"]
        # missing fields -> None / low default
        assert rp.damage_boundary_node_id is None
        assert rp.confidence == "low"
        assert "Partial response." in rp.rationale_md

class _OnlyDerive:
    """Minimal class with only derive (for negative testing)."""

    def derive(self, content: str) -> None:
        return None


class _OnlyReview:
    """Minimal class with only review (for negative testing)."""

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        return None


class TestLoadAgentValidation:
    """Load-time method validation catches incomplete agent classes."""

    def test_missing_review_raises_importerror(self):
        """A class with only derive (no review) raises ImportError at load."""
        from memex.agent import _verify_agent_methods

        with pytest.raises(ImportError, match="review"):
            _verify_agent_methods(_OnlyDerive(), "test:_OnlyDerive")

    def test_missing_derive_raises_importerror(self):
        """A class with only review (no derive) raises ImportError at load."""
        from memex.agent import _verify_agent_methods

        with pytest.raises(ImportError, match="derive"):
            _verify_agent_methods(_OnlyReview(), "test:_OnlyReview")

    def test_both_methods_present_passes(self):
        """A class with both derive and review passes validation."""
        from memex.agent import _verify_agent_methods

        client = load_agent("tests.fake_llm_client:FakeAgent")
        _verify_agent_methods(client, "tests.fake_llm_client:FakeAgent")
        assert hasattr(client, "derive")
        assert hasattr(client, "review")
        assert callable(client.derive)
        assert callable(client.review)
