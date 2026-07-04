"""Tests for `memex review` and `memex review list`.

Relies on the full pipeline: ingest -> derive -> contradicts edge -> review.
LLMClient is injected via MEMEX_LLM_MODULE (FakeLLMClient).
"""
from __future__ import annotations

import json
import uuid

from memex.store import Store as _Store
from tests.conftest import _run_memex, FAKE_FETCHER

FAKE_LLM = "tests.fake_llm_client:FakeLLMClient"
FAKE_LLM_VALID_REFS = "tests.test_review:FakeLLMClientValidRefs"
class FakeLLMClientValidRefs:
    """Fake LLM client returning realistic referencable values.

    Unlike FakeLLMClient (which returns fake node IDs like 'n1','n2'),
    this client returns damage_boundary_node_id=None to satisfy the FK constraint.
    """

    def derive(self, content: str) -> dict:
        return {"prose": "fake", "synthesis_statements": []}

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> dict:
        from memex.llm_client import ReviewProposal
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Fake review: all good.",
            confidence="high",
        )

class FakeLLMClientThrowsOnReview:
    """Fake LLM client that raises on every review() call.

    Used to test per-event error recovery in the review batch command.
    """

    def derive(self, content: str) -> dict:
        return {"prose": "fake", "synthesis_statements": []}

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        raise RuntimeError("Simulated LLM review failure")



def _ingest(store, url: str) -> dict:
    result = _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _derive(store, node_id: str):
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_LLM_MODULE": FAKE_LLM},
    )


class TestReviewCLI:
    """Integration tests for memex review and memex review list."""

    def _add_contradicts_edge(self, store_dict, from_node: str, to_node: str):
        """Open the db and create a contradicts edge to trigger an event."""
        with _Store.open(store_dict["db"]) as s:
            s.init_schema()
            edge_id = str(uuid.uuid4())
            s.create_edge(
                edge_id=edge_id,
                type="association",
                relation="contradicts",
                from_node=from_node,
                to_node=to_node,
            )

    def test_review_full_flow(self, store):
        """Ingest, derive, add contradicts edge, review, assert proposal JSON."""
        ingested = _ingest(store, "https://example.com/article")
        l0_id = ingested["id"]

        derive_result = _derive(store, l0_id)
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        derived_id = derived["id"]

        self._add_contradicts_edge(store, derived_id, l0_id)

        # memex review -- produces proposals
        review_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_LLM_MODULE": FAKE_LLM_VALID_REFS},
        )
        assert review_result.returncode == 0, review_result.stderr
        result_data = json.loads(review_result.stdout)
        assert isinstance(result_data, dict)
        assert result_data["processed"] >= 1
        proposals = result_data["proposals"]
        assert isinstance(proposals, list)
        assert len(proposals) >= 1
        prop = proposals[0]
        assert prop["status"] == "proposed"
        assert "event_id" in prop
        assert "proposal_id" in prop

        # memex review list -- shows proposal
        list_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]), "list"],
            env={"MEMEX_LLM_MODULE": FAKE_LLM},
        )
        assert list_result.returncode == 0, list_result.stderr
        queue = json.loads(list_result.stdout)
        assert isinstance(queue, list)
        pending = [q for q in queue if q.get("kind") == "pending_proposal"]
        assert len(pending) >= 1
        assert pending[0]["id"] == prop["proposal_id"]

    def test_review_is_idempotent(self, store):
        """Re-running review after proposals exist produces no new proposals."""
        ingested = _ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        derive_result = _derive(store, l0_id)
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        derived_id = derived["id"]
        self._add_contradicts_edge(store, derived_id, l0_id)

        result1 = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_LLM_MODULE": FAKE_LLM_VALID_REFS},
        )
        assert result1.returncode == 0, result1.stderr
        data1 = json.loads(result1.stdout)
        proposals1 = data1["proposals"]
        assert len(proposals1) >= 1

        # Re-run -- should return empty (no pending events without proposals)
        result2 = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_LLM_MODULE": FAKE_LLM_VALID_REFS},
        )
        data2 = json.loads(result2.stdout)
        proposals2 = data2["proposals"]
        assert proposals2 == []

    def test_review_no_pending_events_returns_empty(self, store):
        """review with no pending events returns an empty JSON array."""
        result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_LLM_MODULE": FAKE_LLM},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == {"processed": 0, "proposals": []}

    def test_review_list_empty_when_nothing_pending(self, store):
        """review list with no events or proposals returns an empty JSON array."""
        result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]), "list"],
            env={"MEMEX_LLM_MODULE": FAKE_LLM},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == []

    def test_review_llm_error_recovery(self, store):
        """Per-event LLM errors don't crash the batch; each gets status=error."""
        # Set up 2 events so we can verify batch processing continues
        for url in ("https://example.com/a", "https://example.com/b"):
            ingested = _ingest(store, url)
            derive_result = _derive(store, ingested["id"])
            assert derive_result.returncode == 0, derive_result.stderr
            derived = json.loads(derive_result.stdout)
            self._add_contradicts_edge(store, derived["id"], ingested["id"])

        THROWING_LLM = "tests.test_review:FakeLLMClientThrowsOnReview"
        result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_LLM_MODULE": THROWING_LLM},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["processed"] == 2
        proposals = data["proposals"]
        assert len(proposals) == 2
        for entry in proposals:
            assert "event_id" in entry
            assert entry["status"] == "error"
            assert "detail" in entry
