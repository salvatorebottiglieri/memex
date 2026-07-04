"""Tests for the Store module — no subprocess, in-memory SQLite."""
from __future__ import annotations

import sqlite3
import json
import uuid
import pytest
from datetime import datetime, timezone
from pathlib import Path

from memex.store import Store, StoreError


def _store():
    con = sqlite3.connect(":memory:")
    s = Store(con)
    s.init_schema()
    return s


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestSchema:
    def test_init_schema_creates_all_tables(self):
        store = _store()
        tables = store._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in tables if not r[0].startswith("sqlite_")}
        assert names == {"node", "source", "edge", "cursor", "inbox", "event_queue", "event_node_link", "review_proposal"}

    def test_init_schema_is_idempotent(self):
        store = _store()
        store.init_schema()  # second call must not crash
        tables = store._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in tables if not r[0].startswith("sqlite_")}
        assert names == {"node", "source", "edge", "cursor", "inbox", "event_queue", "event_node_link", "review_proposal"}

    def test_init_schema_adds_new_columns(self):
        """Verify is_contested, contested_at on node and written_by on edge exist."""
        store = _store()
        cols_node = {
            r[1] for r in store._con.execute("PRAGMA table_info(node)").fetchall()
        }
        assert "is_contested" in cols_node
        assert "contested_at" in cols_node
        cols_edge = {
            r[1] for r in store._con.execute("PRAGMA table_info(edge)").fetchall()
        }
        assert "written_by" in cols_edge


class TestContestation:
    """Staleness-propagation flow: contestation events + descendant walking."""

    # ── Pyramid builder helper ─────────────────────────────────────

    @staticmethod
    def _build_3_level_pyramid(store) -> tuple[str, str, str, str]:
        """Create L0 <- D1 <- D2 <- D3. Returns (l0, d1, d2, d3)."""
        now = _utcnow()
        l0 = str(uuid.uuid4())
        d1 = str(uuid.uuid4())
        d2 = str(uuid.uuid4())
        d3 = str(uuid.uuid4())
        for nid, kind, depth in [(l0, "raw_source", 0), (d1, "summary", 1),
                                  (d2, "summary", 2), (d3, "summary", 3)]:
            store.create_node(node_id=nid, kind=kind, depth=depth, created_at=now)
        # D1 derived_from L0
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=d1, to_node=l0)
        # D2 derived_from D1
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=d2, to_node=d1)
        # D3 derived_from D2
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=d3, to_node=d2)
        return l0, d1, d2, d3

    @staticmethod
    def _create_node(store) -> str:
        """Create a single node and return its id."""
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0, created_at=_utcnow())
        return nid

    # ── find_provenance_descendants ─────────────────────────────────

    def test_find_descendants_3_level(self):
        store = _store()
        l0, d1, d2, d3 = self._build_3_level_pyramid(store)
        descendants = store.find_provenance_descendants(l0)
        assert sorted(descendants) == sorted([d1, d2, d3])

    def test_find_descendants_mid_pyramid(self):
        store = _store()
        l0, d1, d2, d3 = self._build_3_level_pyramid(store)
        descendants = store.find_provenance_descendants(d1)
        assert sorted(descendants) == sorted([d2, d3])

    def test_find_descendants_none(self):
        store = _store()
        l0, d1, d2, d3 = self._build_3_level_pyramid(store)
        descendants = store.find_provenance_descendants(d3)
        assert descendants == []

    def test_find_descendants_unknown_node(self):
        store = _store()
        assert store.find_provenance_descendants("nonexistent") == []

    def test_find_descendants_ignores_non_provenance_edges(self):
        """Only 'derived_from' edges are walked."""
        store = _store()
        now = _utcnow()
        n1 = str(uuid.uuid4())
        n2 = str(uuid.uuid4())
        n3 = str(uuid.uuid4())
        for nid in (n1, n2, n3):
            store.create_node(node_id=nid, kind="summary", depth=1, created_at=now)
        # related edge — should NOT be walked
        store.create_edge(edge_id=str(uuid.uuid4()), type="association",
                          relation="related", from_node=n2, to_node=n1)
        # contradicts edge — should NOT be walked
        store.create_edge(edge_id=str(uuid.uuid4()), type="association",
                          relation="contradicts", from_node=n3, to_node=n1)
        assert store.find_provenance_descendants(n1) == []

    # ── open_contestation_event ────────────────────────────────────

    def test_open_contestation_event(self):
        store = _store()
        now = _utcnow()
        nid = str(uuid.uuid4())
        eid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0, created_at=now)
        src = str(uuid.uuid4())
        store.create_node(node_id=src, kind="summary", depth=1, created_at=now)
        store.create_edge(edge_id=eid, type="association", relation="related",
                          from_node=src, to_node=nid)
        event_id = store.open_contestation_event(edge_id=eid, target_node_id=nid)
        assert isinstance(event_id, int)
        row = store._con.execute(
            "SELECT event_type, edge_id, target_node_id, status FROM event_queue WHERE id = ?",
            (event_id,),
        ).fetchone()
        assert row["event_type"] == "contradicts_edge_needs_review"
        assert row["edge_id"] == eid
        assert row["target_node_id"] == nid
        assert row["status"] == "pending"

    # ── link_event_to_node ─────────────────────────────────────────

    def test_link_event_to_node(self):
        store = _store()
        now = _utcnow()
        nid = str(uuid.uuid4())
        eid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0, created_at=now)
        src = str(uuid.uuid4())
        store.create_node(node_id=src, kind="summary", depth=1, created_at=now)
        store.create_edge(edge_id=eid, type="association", relation="related",
                          from_node=src, to_node=nid)
        event_id = store.open_contestation_event(edge_id=eid, target_node_id=nid)
        ts = _utcnow()
        store.link_event_to_node(event_id, nid, ts)
        row = store._con.execute(
            "SELECT event_id, node_id, contested_at FROM event_node_link WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert row["event_id"] == event_id
        assert row["node_id"] == nid
        assert row["contested_at"] == ts

    # ── create_edge(relation='contradicts') full flow ──────────────

    def test_contradicts_edge_creates_event_and_links(self):
        """Writing a contradicts edge creates an event_queue row and
        one event_node_link per transitive descendant."""
        store = _store()
        l0, d1, d2, d3 = self._build_3_level_pyramid(store)
        src = str(uuid.uuid4())
        store.create_node(node_id=src, kind="summary", depth=1, created_at=_utcnow())
        edge_id = str(uuid.uuid4())
        store.create_edge(edge_id=edge_id, type="association",
                          relation="contradicts", from_node=src, to_node=l0)
        # One event in event_queue
        row = store._con.execute(
            "SELECT id, event_type, edge_id, target_node_id, status FROM event_queue"
        ).fetchone()
        assert row is not None
        assert row["event_type"] == "contradicts_edge_needs_review"
        assert row["edge_id"] == edge_id
        assert row["target_node_id"] == l0
        assert row["status"] == "pending"
        # event_node_link rows for each descendant and the target node
        links = store._con.execute(
            "SELECT node_id FROM event_node_link WHERE event_id = ? ORDER BY node_id",
            (row["id"],),
        ).fetchall()
        assert {r["node_id"] for r in links} == {l0, d1, d2, d3}

    def test_contradicts_edge_sets_is_contested(self):
        """Nodes covered by a contradiction event have is_contested=1 and contested_at set."""
        store = _store()
        l0, d1, d2, d3 = self._build_3_level_pyramid(store)
        src = str(uuid.uuid4())
        store.create_node(node_id=src, kind="summary", depth=1, created_at=_utcnow())
        edge_id = str(uuid.uuid4())
        store.create_edge(edge_id=edge_id, type="association",
                          relation="contradicts", from_node=src, to_node=l0)
        for nid in (l0, d1, d2, d3):
            node = store.get_node(nid)
            assert node is not None
            assert node["is_contested"] is True
            assert node["contested_at"] is not None

    def test_contradicts_edge_flags_target_node(self):
        """The target node itself (L0) IS flagged as contested,
        along with transitive descendants."""
        store = _store()
        l0, d1, d2, d3 = self._build_3_level_pyramid(store)
        src = str(uuid.uuid4())
        store.create_node(node_id=src, kind="summary", depth=1, created_at=_utcnow())
        edge_id = str(uuid.uuid4())
        store.create_edge(edge_id=edge_id, type="association",
                          relation="contradicts", from_node=src, to_node=l0)
        node = store.get_node(l0)
        assert node is not None
        assert node["is_contested"] is True
        assert node["contested_at"] is not None

    def test_non_contradicts_edges_do_not_create_event(self):
        """derived_from, related, refines do NOT create an event or modify is_contested."""
        store = _store()
        now = _utcnow()
        l0 = str(uuid.uuid4())
        deriv = str(uuid.uuid4())
        store.create_node(node_id=l0, kind="raw_source", depth=0, created_at=now)
        store.create_node(node_id=deriv, kind="summary", depth=1, created_at=now)
        for relation in ("derived_from", "related", "refines"):
            eid = str(uuid.uuid4())
            store.create_edge(edge_id=eid, type="provenance" if relation == "derived_from" else "association",
                              relation=relation, from_node=deriv, to_node=l0)
        count = store._con.execute("SELECT COUNT(*) FROM event_queue").fetchone()[0]
        assert count == 0
        node = store.get_node(deriv)
        assert node is not None
        assert node["is_contested"] is False

    def test_create_edge_persists_written_by(self):
        """create_edge(written_by=...) correctly persists the authorship."""
        store = _store()
        now = _utcnow()
        n1 = str(uuid.uuid4())
        n2 = str(uuid.uuid4())
        store.create_node(node_id=n1, kind="raw_source", depth=0, created_at=now)
        store.create_node(node_id=n2, kind="summary", depth=1, created_at=now)
        eid = str(uuid.uuid4())
        store.create_edge(edge_id=eid, type="provenance", relation="derived_from",
                          from_node=n2, to_node=n1, written_by="llm")
        row = store._con.execute("SELECT written_by FROM edge WHERE id = ?", (eid,)).fetchone()
        assert row is not None
        assert row["written_by"] == "llm"

    def test_create_edge_default_written_by(self):
        """Default written_by is 'human'."""
        store = _store()
        now = _utcnow()
        n1 = str(uuid.uuid4())
        n2 = str(uuid.uuid4())
        store.create_node(node_id=n1, kind="raw_source", depth=0, created_at=now)
        store.create_node(node_id=n2, kind="summary", depth=1, created_at=now)
        eid = str(uuid.uuid4())
        store.create_edge(edge_id=eid, type="provenance", relation="derived_from",
                          from_node=n2, to_node=n1)
        row = store._con.execute("SELECT written_by FROM edge WHERE id = ?", (eid,)).fetchone()
        assert row is not None
        assert row["written_by"] == "human"

    def test_create_edge_invalid_written_by(self):
        """An invalid written_by value raises StoreError (SQLite CHECK constraint)."""
        store = _store()
        now = _utcnow()
        n1 = str(uuid.uuid4())
        n2 = str(uuid.uuid4())
        store.create_node(node_id=n1, kind="raw_source", depth=0, created_at=now)
        store.create_node(node_id=n2, kind="summary", depth=1, created_at=now)
        with pytest.raises(StoreError):
            store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                              relation="derived_from", from_node=n2, to_node=n1,
                              written_by="robot")

    # ── Atomicity ──────────────────────────────────────────────────

    def test_contradicts_flow_rolls_back_on_failure(self, tmp_path: Path):
        """The whole contestation flow is atomic — if it fails, nothing persists."""
        db_path = tmp_path / "test_atomic.db"
        with Store.open(db_path) as store:
            store.init_schema()
            l0, d1, d2, d3 = self._build_3_level_pyramid(store)
        # Reopen with a raw connection to manually manage transactions
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        store2 = Store(con)
        store2.init_schema()
        now = _utcnow()
        l0 = str(uuid.uuid4())
        d1 = str(uuid.uuid4())
        d2 = str(uuid.uuid4())
        d3 = str(uuid.uuid4())
        for nid, kind, depth in [(l0, "raw_source", 0), (d1, "summary", 1),
                                  (d2, "summary", 2), (d3, "summary", 3)]:
            store2.create_node(node_id=nid, kind=kind, depth=depth, created_at=now)
        store2.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                           relation="derived_from", from_node=d1, to_node=l0)
        store2.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                           relation="derived_from", from_node=d2, to_node=d1)
        store2.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                           relation="derived_from", from_node=d3, to_node=d2)
        src = str(uuid.uuid4())
        store2.create_node(node_id=src, kind="summary", depth=1, created_at=now)
        edge_id = str(uuid.uuid4())
        store2.create_edge(edge_id=edge_id, type="association",
                           relation="contradicts", from_node=src, to_node=l0)
        con.rollback()
        event_count = con.execute("SELECT COUNT(*) FROM event_queue").fetchone()[0]
        assert event_count == 0
        link_count = con.execute("SELECT COUNT(*) FROM event_node_link").fetchone()[0]
        assert link_count == 0
        con.close()

    # ── get_node / list_nodes coverage ─────────────────────────────

    def test_get_node_includes_is_contested(self):
        store = _store()
        now = _utcnow()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="summary", depth=1, created_at=now)
        node = store.get_node(nid)
        assert node is not None
        assert "is_contested" in node
        assert "contested_at" in node
        assert node["is_contested"] is False
        assert node["contested_at"] is None

    def test_list_nodes_includes_is_contested(self):
        store = _store()
        now = _utcnow()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="summary", depth=1, created_at=now)
        nodes = store.list_nodes()
        assert len(nodes) == 1
        assert "is_contested" in nodes[0]
        assert "contested_at" in nodes[0]
        assert nodes[0]["is_contested"] is False
        assert nodes[0]["contested_at"] is None



class TestReviewProposal:
    """Tests for review_proposal table operations."""

    @staticmethod
    def _make_contradicts_event(store) -> tuple[int, str, str, str]:
        """Create a contradicts edge that produces a pending event.
        Returns (event_id, edge_id, target_node_id, asserter_node_id)."""
        now = _utcnow()
        target = str(uuid.uuid4())
        store.create_node(node_id=target, kind="raw_source", depth=0, created_at=now,
                          content_path="/tmp/fake.txt")
        asserter = str(uuid.uuid4())
        store.create_node(node_id=asserter, kind="summary", depth=1, created_at=now,
                          content_path="/tmp/fake_assert.txt")
        eid = str(uuid.uuid4())
        store.create_edge(edge_id=eid, type="association", relation="contradicts",
                          from_node=asserter, to_node=target)
        row = store._con.execute(
            "SELECT id FROM event_queue WHERE edge_id = ?", (eid,)
        ).fetchone()
        return (row["id"], eid, target, asserter)

    def test_get_pending_events_without_proposal_returns_event(self):
        store = _store()
        event_id, eid, target, _asserter = self._make_contradicts_event(store)
        events = store.get_pending_events_without_proposal()
        assert len(events) == 1
        assert events[0]["id"] == event_id
        assert events[0]["status"] == "pending"

    def test_get_pending_events_without_proposal_empty_when_no_events(self):
        store = _store()
        assert store.get_pending_events_without_proposal() == []

    def test_get_pending_events_without_proposal_excludes_closed(self):
        store = _store()
        event_id, eid, target, _asserter = self._make_contradicts_event(store)
        store._con.execute("UPDATE event_queue SET status = 'closed' WHERE id = ?", (event_id,))
        assert store.get_pending_events_without_proposal() == []

    def test_get_pending_events_without_proposal_excludes_proposal_exists(self):
        store = _store()
        event_id, eid, target, asserter = self._make_contradicts_event(store)
        store.write_review_proposal(
            event_id=event_id,
            affected_node_ids=[target, asserter],
            damage_boundary_node_id=asserter,
            rationale_md="test rationale",
            confidence="high",
        )
        assert store.get_pending_events_without_proposal() == []

    def test_write_review_proposal_returns_id(self):
        store = _store()
        event_id, eid, target, asserter = self._make_contradicts_event(store)
        pid = store.write_review_proposal(
            event_id=event_id,
            affected_node_ids=[target, asserter],
            damage_boundary_node_id=asserter,
            rationale_md="test rationale",
            confidence="high",
        )
        assert isinstance(pid, int)

    def test_write_review_proposal_persists_fields(self):
        store = _store()
        event_id, eid, target, asserter = self._make_contradicts_event(store)
        pid = store.write_review_proposal(
            event_id=event_id,
            affected_node_ids=[target, asserter],
            damage_boundary_node_id=asserter,
            rationale_md="test rationale",
            confidence="high",
        )
        row = store._con.execute(
            "SELECT event_id, affected_node_ids, damage_boundary_node_id, rationale_md, "
            "confidence, status, human_note, created_at, resolved_at FROM review_proposal WHERE id = ?",
            (pid,),
        ).fetchone()
        assert row["event_id"] == event_id
        assert json.loads(row["affected_node_ids"]) == [target, asserter]
        assert row["damage_boundary_node_id"] == asserter
        assert row["rationale_md"] == "test rationale"
        assert row["confidence"] == "high"
        assert row["status"] == "pending"
        assert row["human_note"] is None
        assert row["resolved_at"] is None
        assert row["created_at"] is not None

    def test_write_review_proposal_accepts_none_damage_boundary(self):
        store = _store()
        event_id, eid, target, _asserter = self._make_contradicts_event(store)
        pid = store.write_review_proposal(
            event_id=event_id,
            affected_node_ids=["n1"],
            damage_boundary_node_id=None,
            rationale_md="no boundary",
            confidence="low",
        )
        row = store._con.execute(
            "SELECT damage_boundary_node_id FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert row["damage_boundary_node_id"] is None

    def test_write_review_proposal_unique_event_id(self):
        store = _store()
        event_id, eid, target, _asserter = self._make_contradicts_event(store)
        store.write_review_proposal(
            event_id=event_id, affected_node_ids=["n1"],
            damage_boundary_node_id=None, rationale_md="first", confidence="high",
        )
        with pytest.raises(StoreError):
            store.write_review_proposal(
                event_id=event_id, affected_node_ids=["n2"],
                damage_boundary_node_id=None, rationale_md="second", confidence="high",
            )

    def test_get_review_queue_returns_pending_events(self):
        store = _store()
        event_id, eid, target, _asserter = self._make_contradicts_event(store)
        queue = store.get_review_queue()
        kinds = [item["kind"] for item in queue]
        assert "pending_event" in kinds

    def test_get_review_queue_returns_pending_proposals(self):
        store = _store()
        event_id, eid, target, _asserter = self._make_contradicts_event(store)
        store.write_review_proposal(
            event_id=event_id, affected_node_ids=["n1"],
            damage_boundary_node_id=None, rationale_md="test", confidence="high",
        )
        queue = store.get_review_queue()
        kinds = [item["kind"] for item in queue]
        assert "pending_proposal" in kinds

    def test_get_review_queue_excludes_non_pending_proposals(self):
        store = _store()
        event_id, eid, target, _asserter = self._make_contradicts_event(store)
        pid = store.write_review_proposal(
            event_id=event_id, affected_node_ids=["n1"],
            damage_boundary_node_id=None, rationale_md="test", confidence="high",
        )
        store._con.execute("UPDATE review_proposal SET status = 'accepted' WHERE id = ?", (pid,))
        queue = store.get_review_queue()
        assert not any(item["kind"] == "pending_proposal" for item in queue)

    def test_get_review_queue_sorted_by_created_at(self):
        store = _store()
        # First event, no proposal
        e1_id, _, _, _ = self._make_contradicts_event(store)
        # Second event with proposal
        e2_id, _, _, _ = self._make_contradicts_event(store)
        store.write_review_proposal(
            event_id=e2_id, affected_node_ids=["n1"],
            damage_boundary_node_id=None, rationale_md="test", confidence="high",
        )
        queue = store.get_review_queue()
        assert len(queue) >= 2
        assert queue[0]["kind"] in ("pending_event", "pending_proposal")
        # Items should be ordered by created_at ascending
        created_ats = [item["created_at"] for item in queue]
        assert created_ats == sorted(created_ats)


class TestReviewAdjudication:
    """Tests for accept_proposal, reject_proposal, dismiss_proposal."""

    @staticmethod
    def _make_contradicts_event(store) -> tuple[int, str, str, str]:
        """Create a contradicts edge that produces a pending event.
        Returns (event_id, edge_id, target_node_id, asserter_node_id)."""
        now = datetime.now(timezone.utc).isoformat()
        target = str(uuid.uuid4())
        store.create_node(node_id=target, kind="raw_source", depth=0, created_at=now,
                          content_path="/tmp/fake.txt")
        asserter = str(uuid.uuid4())
        store.create_node(node_id=asserter, kind="summary", depth=1, created_at=now,
                          content_path="/tmp/fake_assert.txt")
        eid = str(uuid.uuid4())
        store.create_edge(edge_id=eid, type="association", relation="contradicts",
                          from_node=asserter, to_node=target)
        row = store._con.execute(
            "SELECT id FROM event_queue WHERE edge_id = ?", (eid,)
        ).fetchone()
        return (row["id"], eid, target, asserter)

    @staticmethod
    def _make_pending_proposal(store, event_id: int, affected: list[str]) -> int:
        """Create a pending review proposal and return its id."""
        return store.write_review_proposal(
            event_id=event_id,
            affected_node_ids=affected,
            damage_boundary_node_id=affected[0] if affected else None,
            rationale_md="test rationale",
            confidence="high",
        )

    # ── accept_proposal ──────────────────────────────────────────────

    def test_accept_proposal_sets_trust_state_stale(self):
        """Accepting a proposal sets affected nodes to trust_state='stale'."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        affected = [target, asserter]
        pid = self._make_pending_proposal(store, event_id, affected)
        result = store.accept_proposal(pid)
        assert result["status"] == "accepted"
        assert result["proposal_id"] == pid
        assert sorted(result["affected"]) == sorted(affected)
        for nid in affected:
            node = store.get_node(nid)
            assert node is not None
            assert node["trust_state"] == "stale"

    def test_accept_proposal_closes_event_and_proposal(self):
        """After accept, event is closed with closed_at; proposal is accepted with resolved_at."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.accept_proposal(pid)
        # Proposal row
        prop_row = store._con.execute(
            "SELECT status, resolved_at FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert prop_row["status"] == "accepted"
        assert prop_row["resolved_at"] is not None
        # Event row
        evt_row = store._con.execute(
            "SELECT status, closed_at FROM event_queue WHERE id = ?", (event_id,)
        ).fetchone()
        assert evt_row["status"] == "closed"
        assert evt_row["closed_at"] is not None

    def test_accept_proposal_clears_is_contested(self):
        """After accepting the only covering event, nodes become uncontested."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.accept_proposal(pid)
        for nid in (target, asserter):
            node = store.get_node(nid)
            assert node is not None
            assert node["is_contested"] is False
            assert node["contested_at"] is None

    def test_accept_proposal_multi_event_coverage(self):
        """Node covered by 2 events stays contested after first accept, clears after second."""
        store = _store()
        # Create a target node
        now = datetime.now(timezone.utc).isoformat()
        target = str(uuid.uuid4())
        store.create_node(node_id=target, kind="raw_source", depth=0, created_at=now,
                          content_path="/tmp/target.txt")
        # Two contradictors
        a1 = str(uuid.uuid4())
        store.create_node(node_id=a1, kind="summary", depth=1, created_at=now,
                          content_path="/tmp/a1.txt")
        a2 = str(uuid.uuid4())
        store.create_node(node_id=a2, kind="summary", depth=1, created_at=now,
                          content_path="/tmp/a2.txt")
        # Contradiction 1
        e1 = str(uuid.uuid4())
        store.create_edge(edge_id=e1, type="association", relation="contradicts",
                          from_node=a1, to_node=target)
        ev1 = store._con.execute("SELECT id FROM event_queue WHERE edge_id = ?", (e1,)).fetchone()["id"]
        # Contradiction 2
        e2 = str(uuid.uuid4())
        store.create_edge(edge_id=e2, type="association", relation="contradicts",
                          from_node=a2, to_node=target)
        ev2 = store._con.execute("SELECT id FROM event_queue WHERE edge_id = ?", (e2,)).fetchone()["id"]
        # Proposals for both
        p1 = self._make_pending_proposal(store, ev1, [target])
        p2 = self._make_pending_proposal(store, ev2, [target])
        # First accept — target should stay contested (second event still open)
        r1 = store.accept_proposal(p1)
        assert r1["status"] == "accepted"
        node = store.get_node(target)
        assert node is not None
        assert node["is_contested"] is True, "target should still be contested"
        assert r1["still_contested"] == [target]
        # Second accept — target becomes clean
        r2 = store.accept_proposal(p2)
        assert r2["status"] == "accepted"
        node = store.get_node(target)
        assert node is not None
        assert node["is_contested"] is False
        assert r2["still_contested"] == []

    def test_accept_proposal_human_approved_override(self):
        """Human-approved nodes get set to stale just like any other trust_state."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        store._con.execute(
            "UPDATE node SET trust_state = 'human-approved' WHERE id = ?", (target,)
        )
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.accept_proposal(pid)
        node = store.get_node(target)
        assert node is not None
        assert node["trust_state"] == "stale"

    def test_accept_proposal_idempotent(self):
        """Second accept returns already_resolved with correct current_status."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        r1 = store.accept_proposal(pid)
        assert r1["status"] == "accepted"
        r2 = store.accept_proposal(pid)
        assert r2["status"] == "already_resolved"
        assert r2["current_status"] == "accepted"
        # No re-execution: event_node_link should still be empty
        links = store._con.execute(
            "SELECT COUNT(*) FROM event_node_link WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        assert links == 0

    def test_accept_proposal_raises_store_error_on_db_failure(self):
        """StoreError wraps db failures during accept."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])

        class _FailingConnection:
            def __init__(self, conn, fail_after):
                self._conn = conn
                self._count = 0
                self._fail_after = fail_after
            def execute(self, sql, params=None):
                self._count += 1
                if self._count >= self._fail_after:
                    raise sqlite3.OperationalError("simulated failure")
                if params is not None:
                    return self._conn.execute(sql, params)
                return self._conn.execute(sql)
            def __getattr__(self, name):
                return getattr(self._conn, name)

        real_con = store._con
        store._con = _FailingConnection(real_con, fail_after=3)
        with pytest.raises(StoreError):
            store.accept_proposal(pid)
        # Proposal remains pending — final UPDATE never ran
        prop_row = real_con.execute(
            "SELECT status FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert prop_row["status"] == "pending"
        evt_row = real_con.execute(
            "SELECT status FROM event_queue WHERE id = ?", (event_id,)
        ).fetchone()
        assert evt_row["status"] == "pending"

    # ── reject_proposal ──────────────────────────────────────────────

    def test_reject_proposal_does_not_modify_trust_state(self):
        """Rejecting a proposal does NOT change trust_state."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        affected = [target, asserter]
        pid = self._make_pending_proposal(store, event_id, affected)
        original_states = {}
        for nid in affected:
            node = store.get_node(nid)
            original_states[nid] = node["trust_state"] if node else None
        store.reject_proposal(pid)
        for nid in affected:
            node = store.get_node(nid)
            assert node is not None
            assert node["trust_state"] == original_states[nid], \
                f"{nid} trust_state should not change on reject"

    def test_reject_proposal_clears_is_contested(self):
        """Reject still clears is_contested via _close_contestation_event."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        result = store.reject_proposal(pid)
        assert result["uncontested"] == [target]
        for nid in (target, asserter):
            node = store.get_node(nid)
            assert node is not None
        node = store.get_node(target)
        assert node is not None
        assert node["is_contested"] is False
        assert node["contested_at"] is None

    def test_reject_proposal_closes_event_and_proposal(self):
        """Reject sets proposal status=rejected and event status=closed."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        result = store.reject_proposal(pid)
        assert result["uncontested"] == [target]
        assert result["status"] == "rejected"
        assert result["proposal_id"] == pid
        prop_row = store._con.execute(
            "SELECT status, resolved_at FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert prop_row["status"] == "rejected"
        assert prop_row["resolved_at"] is not None
        evt_row = store._con.execute(
            "SELECT status, closed_at FROM event_queue WHERE id = ?", (event_id,)
        ).fetchone()
        assert evt_row["status"] == "closed"
        assert evt_row["closed_at"] is not None

    def test_reject_proposal_stores_human_note(self):
        """human_note is stored when provided."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.reject_proposal(pid, human_note="Not convincing")
        row = store._con.execute(
            "SELECT human_note FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert row["human_note"] == "Not convincing"

    def test_reject_proposal_idempotent(self):
        """Second reject returns already_resolved with correct current_status."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.reject_proposal(pid)
        r2 = store.reject_proposal(pid)
        assert r2["status"] == "already_resolved"
        assert r2["current_status"] == "rejected"

    def test_reject_proposal_raises_store_error_on_db_failure(self):
        """StoreError wraps db failures during reject."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])

        class _FailingConnection:
            def __init__(self, conn, fail_after):
                self._conn = conn
                self._count = 0
                self._fail_after = fail_after
            def execute(self, sql, params=None):
                self._count += 1
                if self._count >= self._fail_after:
                    raise sqlite3.OperationalError("simulated failure")
                if params is not None:
                    return self._conn.execute(sql, params)
                return self._conn.execute(sql)
            def __getattr__(self, name):
                return getattr(self._conn, name)

        real_con = store._con
        store._con = _FailingConnection(real_con, fail_after=3)
        with pytest.raises(StoreError):
            store.reject_proposal(pid)
        prop_row = real_con.execute(
            "SELECT status FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert prop_row["status"] == "pending"

    def test_reject_proposal_preserves_human_approved(self):
        """Reject does NOT touch trust_state — human-approved stays."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        store._con.execute(
            "UPDATE node SET trust_state = 'human-approved' WHERE id = ?", (target,)
        )
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.reject_proposal(pid)
        node = store.get_node(target)
        assert node is not None
        assert node["trust_state"] == "human-approved"

    # ── dismiss_proposal ─────────────────────────────────────────────

    def test_dismiss_proposal_does_not_modify_trust_state(self):
        """Dismissing does NOT change trust_state."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        original = {}
        for nid in (target, asserter):
            node = store.get_node(nid)
            original[nid] = node["trust_state"] if node else None
        store.dismiss_proposal(pid)
        for nid in (target, asserter):
            node = store.get_node(nid)
            assert node is not None
            assert node["trust_state"] == original[nid]

    def test_dismiss_proposal_uses_dismissed_status(self):
        """Dismiss sets proposal status='dismissed'."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        result = store.dismiss_proposal(pid)
        assert result["status"] == "dismissed"
        assert result["proposal_id"] == pid
        assert result["uncontested"] == [target]
        prop_row = store._con.execute(
            "SELECT status FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert prop_row["status"] == "dismissed"

    def test_dismiss_proposal_idempotent(self):
        """Second dismiss returns already_resolved with correct current_status."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.dismiss_proposal(pid)
        r2 = store.dismiss_proposal(pid)
        assert r2["status"] == "already_resolved"
        assert r2["current_status"] == "dismissed"

    def test_dismiss_proposal_stores_human_note(self):
        """human_note stored on dismiss."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])
        store.dismiss_proposal(pid, human_note="Spam")
        row = store._con.execute(
            "SELECT human_note FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert row["human_note"] == "Spam"

    def test_dismiss_proposal_empty_affected(self):
        """Dismiss with empty affected list still works."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        # Create a proposal with empty affected
        pid = store.write_review_proposal(
            event_id=event_id,
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="empty",
            confidence="low",
        )
        result = store.dismiss_proposal(pid)
        assert result["status"] == "dismissed"
        assert result["uncontested"] == [target]  # target is linked via event_node_link
        evt_row = store._con.execute(
            "SELECT status FROM event_queue WHERE id = ?", (event_id,)
        ).fetchone()
        assert evt_row["status"] == "closed"
        # is_contested should still be recomputed — target and asserter
        # are linked to the event through event_node_link
        node = store.get_node(target)
        assert node is not None
        assert node["is_contested"] is False

    def test_dismiss_proposal_atomic(self):
        """Mid-flight failure rolls back all changes."""
        store = _store()
        event_id, _eid, target, asserter = self._make_contradicts_event(store)
        pid = self._make_pending_proposal(store, event_id, [target, asserter])

        class _FailingConnection:
            def __init__(self, conn, fail_after):
                self._conn = conn
                self._count = 0
                self._fail_after = fail_after
            def execute(self, sql, params=None):
                self._count += 1
                if self._count >= self._fail_after:
                    raise sqlite3.OperationalError("simulated failure")
                if params is not None:
                    return self._conn.execute(sql, params)
                return self._conn.execute(sql)
            def __getattr__(self, name):
                return getattr(self._conn, name)

        store._con = _FailingConnection(store._con, fail_after=6)
        with pytest.raises(StoreError):
            store.dismiss_proposal(pid)
        real_con = store._con._conn
        prop_row = real_con.execute(
            "SELECT status FROM review_proposal WHERE id = ?", (pid,)
        ).fetchone()
        assert prop_row["status"] == "pending"
        evt_row = real_con.execute(
            "SELECT status FROM event_queue WHERE id = ?", (event_id,)
        ).fetchone()
        assert evt_row["status"] == "pending"

class TestLedger:
    def test_lookup_returns_none_for_unknown_key(self):
        store = _store()
        assert store.lookup_by_canonical_key("https://x.com") is None

    def test_lookup_returns_node_id_and_failed_flag(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source")
        store.attach_source(node_id="n1", canonical_key="https://a.com", source_url="https://a.com")
        result = store.lookup_by_canonical_key("https://a.com")
        assert result == {"node_id": "n1", "failed": False}

    def test_lookup_shows_failed_flag(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source")
        store.attach_source(node_id="n1", canonical_key="https://a.com", source_url="https://a.com", failed=True)
        result = store.lookup_by_canonical_key("https://a.com")
        assert result == {"node_id": "n1", "failed": True}


class TestNode:
    def test_create_node_with_defaults(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source")
        n = store.get_node("n1")
        assert n is not None
        assert n["kind"] == "raw_source"
        assert n["depth"] == 0
        assert n["trust_state"] == "draft"
        assert n["content_path"] == ""
        assert n["created_at"] is not None

    def test_get_node_returns_none_for_missing(self):
        store = _store()
        assert store.get_node("nonexistent") is None


class TestList:
    def test_list_returns_empty_on_fresh_db(self):
        store = _store()
        assert store.list_nodes() == []

    def test_list_nodes_ordered_by_created_at(self):
        store = _store()
        store.create_node(node_id="n2", kind="summary", depth=1, created_at="2024-01-02")
        store.create_node(node_id="n1", kind="raw_source", depth=0, created_at="2024-01-01")
        nodes = store.list_nodes()
        assert nodes[0]["id"] == "n1"
        assert nodes[1]["id"] == "n2"


class TestEdgeCursor:
    def test_edge_methods_exist(self):
        store = _store()
        for name in ("create_edge", "list_edges", "find_provenance_descendants",
                     "open_contestation_event", "link_event_to_node"):
            assert callable(getattr(store, name)), f"store.{name} is not callable"

    def test_cursor_methods_exist(self):
        store = _store()
        for name in ("get_cursor", "set_cursor"):
            assert callable(getattr(store, name)), f"store.{name} is not callable"
