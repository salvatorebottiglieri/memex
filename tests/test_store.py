"""Tests for the Store module — no subprocess, in-memory SQLite."""
from __future__ import annotations

import sqlite3
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
        assert names == {"node", "source", "edge", "cursor", "inbox", "event_queue", "event_node_link"}

    def test_init_schema_is_idempotent(self):
        store = _store()
        store.init_schema()  # second call must not crash
        tables = store._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in tables if not r[0].startswith("sqlite_")}
        assert names == {"node", "source", "edge", "cursor", "inbox", "event_queue", "event_node_link"}

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
