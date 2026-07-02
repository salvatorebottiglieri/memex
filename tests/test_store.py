"""Tests for the Store module — no subprocess, in-memory SQLite."""
from __future__ import annotations

import sqlite3

from memex.store import Store, StoreError


def _store():
    con = sqlite3.connect(":memory:")
    s = Store(con)
    s.init_schema()
    return s


class TestSchema:
    def test_init_schema_creates_all_tables(self):
        store = _store()
        tables = store._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in tables if not r[0].startswith("sqlite_")}
        assert names == {"node", "source", "edge", "cursor", "inbox"}

    def test_init_schema_is_idempotent(self):
        store = _store()
        store.init_schema()  # second call must not crash
        tables = store._con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in tables if not r[0].startswith("sqlite_")}
        assert names == {"node", "source", "edge", "cursor", "inbox"}


class TestLedger:
    def test_lookup_returns_none_for_unknown_key(self):
        store = _store()
        assert store.lookup_by_canonical_key("https://x.com") is None

    def test_lookup_returns_node_id_and_failed_flag(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source")
        store.attach_source(node_id="n1", canonical_key="https://x.com", source_url="https://x.com")
        result = store.lookup_by_canonical_key("https://x.com")
        assert result == {"node_id": "n1", "failed": False}

    def test_lookup_shows_failed_flag(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source")
        store.attach_source(node_id="n1", canonical_key="https://x.com", source_url="https://x.com", failed=True)
        result = store.lookup_by_canonical_key("https://x.com")
        assert result == {"node_id": "n1", "failed": True}


class TestNode:
    def test_create_node_with_defaults(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source")
        node = store.get_node("n1")
        assert node["kind"] == "raw_source"
        assert node["trust_state"] == "draft"
        assert node["depth"] == 0
        assert node["tier"] is None

    def test_create_node_with_custom_values(self):
        store = _store()
        store.create_node(node_id="n1", kind="derivation", tier="synthesis",
                          trust_state="human-approved", depth=3,
                          content_path="/vault/n1.md", created_at="2026-01-01")
        node = store.get_node("n1")
        assert node["kind"] == "derivation"
        assert node["tier"] == "synthesis"
        assert node["trust_state"] == "human-approved"
        assert node["depth"] == 3

    def test_get_node_returns_none_for_missing(self):
        store = _store()
        assert store.get_node("nonexistent") is None


class TestList:
    def test_list_returns_empty_on_fresh_db(self):
        store = _store()
        assert store.list_nodes() == []

    def test_list_returns_all_nodes(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source")
        store.attach_source(node_id="n1", canonical_key="https://a.com", source_url="https://a.com")
        store.create_node(node_id="n2", kind="raw_source")
        store.attach_source(node_id="n2", canonical_key="https://b.com", source_url="https://b.com")
        nodes = store.list_nodes()
        assert len(nodes) == 2
        assert nodes[0]["id"] == "n1"
        assert nodes[1]["id"] == "n2"


class TestEdgeCursorStubs:
    """Edges and cursors are not yet implemented — verify they have the
    right interface shape so the module is future-proof."""

    def test_edge_methods_exist(self):
        store = _store()
        assert hasattr(store, "create_edge")
        assert hasattr(store, "list_edges")

    def test_cursor_methods_exist(self):
        store = _store()
        assert hasattr(store, "get_cursor")
        assert hasattr(store, "set_cursor")
