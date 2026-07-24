"""Tests for `memex review` and `memex review list`.

Relies on the full pipeline: register -> derive -> contradicts edge -> review.
Agent is injected via MEMEX_AGENT (FakeAgent).
"""
from __future__ import annotations

import json
import uuid

from memex.store import Store as _Store
from tests.conftest import _run_memex, register_node, WORKTREE

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_AGENT_VALID_REFS = "tests.fake_llm_client:FakeAgentValidRefs"



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
        """Register, derive, add contradicts edge, review, assert proposal JSON."""
        r = register_node(store, store["vault"], "review-flow.md",
                          "https://example.com/article")
        l0_id = json.loads(r.stdout)["id"]

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
        r = register_node(store, store["vault"], "review-idem.md",
                          "https://example.com/article")
        l0_id = json.loads(r.stdout)["id"]
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
            filename = url.rsplit("/", 1)[-1] + ".md"
            r = register_node(store, store["vault"], filename, url)
            node_id = json.loads(r.stdout)["id"]
            derive_result = _derive(store, node_id)
            assert derive_result.returncode == 0, derive_result.stderr
            derived = json.loads(derive_result.stdout)
            self._add_contradicts_edge(store, derived["id"], node_id)

        THROWING_AGENT = "tests.fake_llm_client:FakeAgentThrowsOnReview"
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
        r = register_node(store, store["vault"], "accept-test.md",
                          "https://example.com/accept-test")
        node_id = json.loads(r.stdout)["id"]
        derive_result = _derive(store, node_id)
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], node_id)
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
        r = register_node(store, store["vault"], "reject-test.md",
                          "https://example.com/reject-test")
        node_id = json.loads(r.stdout)["id"]
        derive_result = _derive(store, node_id)
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], node_id)
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
        r = register_node(store, store["vault"], "dismiss-test.md",
                          "https://example.com/dismiss-test")
        node_id = json.loads(r.stdout)["id"]
        derive_result = _derive(store, node_id)
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], node_id)
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
        r = register_node(store, store["vault"], "accept-idem.md",
                          "https://example.com/accept-idem")
        node_id = json.loads(r.stdout)["id"]
        derive_result = _derive(store, node_id)
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], node_id)
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
        r = register_node(store, store["vault"], "no-note.md",
                          "https://example.com/no-note")
        node_id = json.loads(r.stdout)["id"]
        derive_result = _derive(store, node_id)
        assert derive_result.returncode == 0, derive_result.stderr
        derived = json.loads(derive_result.stdout)
        self._add_contradicts_edge(store, derived["id"], node_id)
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
