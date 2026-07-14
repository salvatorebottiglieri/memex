"""Tests for `memex review` and `memex review list`.

Relies on the full pipeline: ingest -> derive -> contradicts edge -> review.
Agent is injected via MEMEX_AGENT (FakeAgent).
"""
from __future__ import annotations

import json
import uuid

from memex.store import Store as _Store
from tests.conftest import _run_memex, FAKE_FETCHER

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_AGENT_VALID_REFS = "tests.test_review:FakeAgentValidRefs"
class FakeAgentValidRefs:
    """Fake agent returning realistic referencable values.

    Unlike FakeAgent (which returns fake node IDs like 'n1','n2'),
    this agent returns damage_boundary_node_id=None to satisfy the FK constraint.
    """

    def derive(self, content: str) -> dict:
        return {"prose": "fake", "synthesis_statements": []}


    def extract_ideas(self, content: str) -> list[str]:
        return ["Idea 1", "Idea 2"]
    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> dict:
        from memex.agent import ReviewProposal
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Fake review: all good.",
            confidence="high",
        )

class FakeAgentThrowsOnReview:
    """Fake agent that raises on every review() call.

    Used to test per-event error recovery in the review batch command.
    """

    def derive(self, content: str) -> dict:
        return {"prose": "fake", "synthesis_statements": []}

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> None:
        raise RuntimeError("Simulated LLM review failure")



    def extract_ideas(self, content: str) -> list[str]:
        return ["Idea 1", "Idea 2"]

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
        env={"MEMEX_AGENT": FAKE_AGENT},
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
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
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
            env={"MEMEX_AGENT": FAKE_AGENT},
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
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
        )
        assert result1.returncode == 0, result1.stderr
        data1 = json.loads(result1.stdout)
        proposals1 = data1["proposals"]
        assert len(proposals1) >= 1

        # Re-run -- should return empty (no pending events without proposals)
        result2 = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
        )
        data2 = json.loads(result2.stdout)
        proposals2 = data2["proposals"]
        assert proposals2 == []

    def test_review_no_pending_events_returns_empty(self, store):
        """review with no pending events returns an empty JSON array."""
        result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == {"processed": 0, "proposals": []}

    def test_review_list_empty_when_nothing_pending(self, store):
        """review list with no events or proposals returns an empty JSON array."""
        result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]), "list"],
            env={"MEMEX_AGENT": FAKE_AGENT},
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

        THROWING_AGENT = "tests.test_review:FakeAgentThrowsOnReview"
        result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": THROWING_AGENT},
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

    # ── accept / reject / dismiss ──────────────────────────────────

    def test_review_accept_full_flow(self, store):
        """memex review accept <id> with --note."""
        # Set up an event and proposal via the full pipeline
        ingested = _ingest(store, "https://example.com/accept-test")
        derive_result = _derive(store, ingested["id"])
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], ingested["id"])
        # Generate proposal
        review_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
        )
        assert review_result.returncode == 0, review_result.stderr
        data = json.loads(review_result.stdout)
        assert data["processed"] >= 1
        prop = data["proposals"][0]
        pid = prop["proposal_id"]
        # Accept
        accept_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "accept", str(pid), "--note", "Looks good"],
        )
        assert accept_result.returncode == 0, accept_result.stderr
        accept_data = json.loads(accept_result.stdout)
        assert accept_data["status"] == "accepted"
        assert accept_data["proposal_id"] == pid
        # Verify via store
        with _Store.open(store["db"]) as s:
            row = s._con.execute(
                "SELECT status, human_note FROM review_proposal WHERE id = ?", (pid,)
            ).fetchone()
            assert row["status"] == "accepted"
            assert row["human_note"] == "Looks good"

    def test_review_reject_full_flow(self, store):
        """memex review reject <id> with --note."""
        ingested = _ingest(store, "https://example.com/reject-test")
        derive_result = _derive(store, ingested["id"])
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], ingested["id"])
        review_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
        )
        assert review_result.returncode == 0, review_result.stderr
        prop = json.loads(review_result.stdout)["proposals"][0]
        pid = prop["proposal_id"]
        reject_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "reject", str(pid), "--note", "Not needed"],
        )
        assert reject_result.returncode == 0, reject_result.stderr
        reject_data = json.loads(reject_result.stdout)
        assert reject_data["status"] == "rejected"
        assert reject_data["proposal_id"] == pid
        with _Store.open(store["db"]) as s:
            row = s._con.execute(
                "SELECT status, human_note FROM review_proposal WHERE id = ?", (pid,)
            ).fetchone()
            assert row["status"] == "rejected"
            assert row["human_note"] == "Not needed"

    def test_review_dismiss_full_flow(self, store):
        """memex review dismiss <id> with --note."""
        ingested = _ingest(store, "https://example.com/dismiss-test")
        derive_result = _derive(store, ingested["id"])
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], ingested["id"])
        review_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
        )
        assert review_result.returncode == 0, review_result.stderr
        prop = json.loads(review_result.stdout)["proposals"][0]
        pid = prop["proposal_id"]
        dismiss_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "dismiss", str(pid), "--note", "Off-topic"],
        )
        assert dismiss_result.returncode == 0, dismiss_result.stderr
        dismiss_data = json.loads(dismiss_result.stdout)
        assert dismiss_data["status"] == "dismissed"
        assert dismiss_data["proposal_id"] == pid
        with _Store.open(store["db"]) as s:
            row = s._con.execute(
                "SELECT status, human_note FROM review_proposal WHERE id = ?", (pid,)
            ).fetchone()
            assert row["status"] == "dismissed"
            assert row["human_note"] == "Off-topic"

    def test_review_accept_idempotent(self, store):
        """Second accept returns already_resolved."""
        ingested = _ingest(store, "https://example.com/accept-idem")
        derive_result = _derive(store, ingested["id"])
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], ingested["id"])
        review_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
        )
        assert review_result.returncode == 0, review_result.stderr
        pid = json.loads(review_result.stdout)["proposals"][0]["proposal_id"]
        # First accept
        r1 = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "accept", str(pid)],
        )
        assert r1.returncode == 0, r1.stderr
        assert json.loads(r1.stdout)["status"] == "accepted"
        # Second accept
        r2 = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "accept", str(pid)],
        )
        assert r2.returncode == 0, r2.stderr
        assert json.loads(r2.stdout)["status"] == "already_resolved"
        assert json.loads(r2.stdout)["current_status"] == "accepted"

    def test_review_accept_without_note(self, store):
        """Accept without --note stores NULL human_note."""
        ingested = _ingest(store, "https://example.com/no-note")
        derive_result = _derive(store, ingested["id"])
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], ingested["id"])
        review_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT_VALID_REFS},
        )
        assert review_result.returncode == 0, review_result.stderr
        pid = json.loads(review_result.stdout)["proposals"][0]["proposal_id"]
        accept_result = _run_memex(
            ["review", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "accept", str(pid)],
        )
        assert accept_result.returncode == 0, accept_result.stderr
        with _Store.open(store["db"]) as s:
            row = s._con.execute(
                "SELECT human_note FROM review_proposal WHERE id = ?", (pid,)
            ).fetchone()
            assert row["human_note"] is None
