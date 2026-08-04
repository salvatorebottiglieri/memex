"""Tests for ticket #95 — URL-node and extracted-node store kinds.

Covers: create_node kind='url' / kind='extracted', provenance edge
extracted -> URL-node, fetcher_type persistence and confidence map,
the contestation guard for URL nodes, and the nullable-schema migration.
"""
from __future__ import annotations

import sqlite3
import uuid

import pytest

from memex.store import Store, StoreError

from tests.conftest import _store, _utcnow


def _url_node(store, node_id=None) -> str:
    node_id = node_id or str(uuid.uuid4())
    store.create_node(node_id=node_id, kind="url")
    return node_id


def _extracted_node(store, url_id: str, *, fetcher_type="http",
                    content_path=None) -> str:
    node_id = str(uuid.uuid4())
    store.create_node(
        node_id=node_id,
        kind="extracted",
        fetcher_type=fetcher_type,
        content_path=content_path or f"/tmp/{node_id}.md",
        derived_from=url_id,
    )
    return node_id


# ── Schema ──────────────────────────────────────────────────────────────

class TestSchema:
    def test_fresh_schema_has_fetcher_type_and_nullable_columns(self):
        """Fresh DBs: fetcher_type column exists; trust_state/content_path nullable."""
        store = _store()
        info = {
            r[1]: r for r in store._con.execute("PRAGMA table_info(node)").fetchall()
        }
        assert "fetcher_type" in info
        assert info["trust_state"]["notnull"] == 0
        assert info["content_path"]["notnull"] == 0

    def test_fresh_db_skips_rebuild(self):
        """Fresh DBs get the new schema from _SCHEMA_SQL — the rebuild guard
        engages (nullable columns + fetcher_type present) and no DROP/recreate
        runs on first init_schema.

        Observable: the rebuild rebuilds ``node`` from ``node_new``, which
        declares every migrated column inline — ``fetcher_type`` ends up the
        last column (index 12). A fresh, un-rebuilt table has ``fetcher_type``
        in the base _SCHEMA_SQL definition (index 7), with the later ALTER
        TABLE columns appended after it."""
        store = _store()
        cols = [
            r[1] for r in store._con.execute("PRAGMA table_info(node)").fetchall()
        ]
        assert cols.index("fetcher_type") == 7
        # Sanity: the rebuild target layout would put it last.
        assert cols.index("fetcher_type") != len(cols) - 1

    def test_migration_makes_columns_nullable_and_adds_fetcher_type(self, tmp_path):
        """Old-schema DB (NOT NULL trust_state/content_path) migrates via rebuild."""
        db = self._old_schema_db(tmp_path)
        with Store.open(db) as store:
            store.init_schema()
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        info = {
            r[1]: r for r in con.execute("PRAGMA table_info(node)").fetchall()
        }
        con.close()
        assert info["trust_state"]["notnull"] == 0
        assert info["content_path"]["notnull"] == 0
        assert "fetcher_type" in info

    def test_migration_preserves_existing_node_data(self, tmp_path):
        """Existing rows survive the rebuild (trust_state/content_path preserved)."""
        db = self._old_schema_db(tmp_path)
        con = sqlite3.connect(db)
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at) "
            "VALUES ('n1', 'summary', 'notes', 'human-approved', 1, '/tmp/note.md', '2024-01-01T00:00:00')"
        )
        con.commit()
        con.close()
        with Store.open(db) as store:
            store.init_schema()
        with Store.open(db) as store:
            node = store.get_node("n1")
        assert node is not None
        assert node["trust_state"] == "human-approved"
        assert node["content_path"] == "/tmp/note.md"
        assert node["tier"] == "notes"

    def test_migration_preserves_confidence_column_data(self, tmp_path):
        """Intermediate-schema DB (confidence column with data) keeps confidence."""
        db = self._old_schema_db(tmp_path)
        con = sqlite3.connect(db)
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            "ALTER TABLE node ADD COLUMN confidence TEXT CHECK (confidence IN ('high','medium','low'))"
        )
        con.execute(
            "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at, confidence) "
            "VALUES ('n1', 'summary', 'synthesis', 'draft', 2, '/tmp/syn.md', '2024-01-01T00:00:00', 'high')"
        )
        con.commit()
        con.close()
        with Store.open(db) as store:
            store.init_schema()
        with Store.open(db) as store:
            node = store.get_node("n1")
        assert node is not None
        assert node["confidence"] == "high"
        assert node["content_path"] == "/tmp/syn.md"

    def test_rebuild_failure_rolls_back_atomically(self, tmp_path, monkeypatch):
        """A failure after the rebuild leaves the DB untouched (old schema +
        data intact), and re-running init_schema recovers (idempotent)."""
        db = self._old_schema_db(tmp_path)
        # Old-schema DB with data referencing the node table, so the rebuild
        # depends on foreign_keys being disabled around the DROP TABLE.
        con = sqlite3.connect(db)
        con.execute("PRAGMA foreign_keys = ON")
        con.execute(
            "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at) "
            "VALUES ('n1', 'summary', 'notes', 'human-approved', 1, '/tmp/note.md', '2024-01-01T00:00:00')"
        )
        con.execute(
            "INSERT INTO source (node_id, canonical_key, source_url, title, fetched_at) "
            "VALUES ('n1', 'ck1', 'https://example.com/x', 'T', NULL)"
        )
        con.execute(
            "INSERT INTO edge (id, type, relation, from_node, to_node) "
            "VALUES ('e1', 'provenance', 'derived_from', 'n1', 'n1')"
        )
        con.commit()
        con.close()

        def boom(self):
            raise RuntimeError("backfill exploded")

        with monkeypatch.context() as m:
            m.setattr(Store, "_backfill_confidence", boom)
            with pytest.raises(RuntimeError, match="backfill exploded"):
                with Store.open(db) as store:
                    store.init_schema()

        # Nothing was committed: still the old schema, data intact.
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        info = {
            r[1]: r for r in con.execute("PRAGMA table_info(node)").fetchall()
        }
        rows = con.execute(
            "SELECT id, trust_state FROM node WHERE id = 'n1'"
        ).fetchall()
        source_rows = con.execute(
            "SELECT node_id FROM source WHERE node_id = 'n1'"
        ).fetchall()
        con.close()
        assert info["trust_state"]["notnull"] == 1
        assert info["content_path"]["notnull"] == 1
        assert "fetcher_type" not in info
        assert len(rows) == 1 and rows[0]["trust_state"] == "human-approved"
        assert len(source_rows) == 1

        # Idempotent recovery: a plain re-run migrates successfully.
        with Store.open(db) as store:
            store.init_schema()
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        info = {
            r[1]: r for r in con.execute("PRAGMA table_info(node)").fetchall()
        }
        node = con.execute(
            "SELECT id, trust_state FROM node WHERE id = 'n1'"
        ).fetchone()
        con.close()
        assert info["trust_state"]["notnull"] == 0
        assert info["content_path"]["notnull"] == 0
        assert "fetcher_type" in info
        assert node is not None and node["trust_state"] == "human-approved"

    def test_migrated_db_supports_url_and_extracted(self, tmp_path):
        """The full new model works on a migrated DB."""
        db = self._old_schema_db(tmp_path)
        with Store.open(db) as store:
            store.init_schema()
            url_id = str(uuid.uuid4())
            store.create_node(node_id=url_id, kind="url")
            ext_id = str(uuid.uuid4())
            store.create_node(
                node_id=ext_id, kind="extracted", fetcher_type="http",
                content_path="/tmp/e.md", derived_from=url_id,
            )
            store.attach_source(
                node_id=url_id, canonical_key="https://example.com/m",
                source_url="https://example.com/m",
            )
        with Store.open(db) as store:
            url = store.get_node(url_id)
            ext = store.get_node(ext_id)
        assert url is not None and url["kind"] == "url"
        assert url["trust_state"] is None
        assert ext is not None and ext["tier"] == "extracted"
        assert ext["depth"] == 1
        assert ext["canonical_key"] is None

    @staticmethod
    def _old_schema_db(tmp_path) -> str:
        """Create a DB with the pre-#95 schema (NOT NULL trust_state/content_path)."""
        db = tmp_path / "old.db"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE node (
                id           TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                tier         TEXT,
                trust_state  TEXT NOT NULL,
                depth        INTEGER NOT NULL,
                content_path TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE TABLE source (
                node_id       TEXT PRIMARY KEY REFERENCES node(id),
                canonical_key TEXT NOT NULL UNIQUE,
                source_url    TEXT NOT NULL,
                title         TEXT,
                fetched_at    TEXT
            );
            CREATE TABLE edge (
                id        TEXT PRIMARY KEY,
                type      TEXT NOT NULL,
                relation  TEXT NOT NULL,
                from_node TEXT NOT NULL REFERENCES node(id),
                to_node   TEXT NOT NULL REFERENCES node(id)
            );
        """)
        con.commit()
        con.close()
        return str(db)


# ── URL nodes ──────────────────────────────────────────────────────────

class TestCreateUrlNode:
    """AC1: kind='url' — NULLs, depth=0, never contested."""

    def test_url_node_stores_nulls_and_depth_zero(self):
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="url")
        node = store.get_node(nid)
        assert node is not None
        assert node["kind"] == "url"
        assert node["tier"] is None
        assert node["trust_state"] is None
        assert node["confidence"] is None
        assert node["content_path"] is None
        assert node["depth"] == 0

    def test_url_node_ignores_contradictory_input(self):
        """Kind='url' coerces tier/trust_state/confidence/content_path/depth."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(
            node_id=nid, kind="url",
            tier="notes", trust_state="human-approved", depth=5,
            content_path="/tmp/nope.md", confidence="high",
        )
        node = store.get_node(nid)
        assert node["tier"] is None
        assert node["trust_state"] is None
        assert node["confidence"] is None
        assert node["content_path"] is None
        assert node["depth"] == 0

    def test_url_node_ignores_synthesis_and_fetcher_type(self):
        """Zero-content invariant: LLM-derived statements and fetcher metadata
        are cleared on url nodes, whatever the caller passed."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(
            node_id=nid, kind="url",
            synthesis_statements=["LLM derived synthesis"],
            fetcher_type="http",
        )
        node = store.get_node(nid)
        assert node["synthesis_statements"] is None
        assert node["fetcher_type"] is None

    def test_url_node_has_no_confidence(self):
        store = _store()
        nid = _url_node(store)
        assert store.compute_node_confidence(nid) is None

    def test_url_node_backfill_keeps_no_confidence(self):
        """_backfill_confidence must never assign confidence to URL nodes."""
        store = _store()
        nid = _url_node(store)
        store._backfill_confidence()
        assert store.get_node(nid)["confidence"] is None

    def test_url_node_cannot_be_contested(self):
        """A contradicts edge targeting a URL node is rejected atomically."""
        store = _store()
        url_id = _url_node(store)
        asserter = str(uuid.uuid4())
        store.create_node(node_id=asserter, kind="summary", depth=1)
        with pytest.raises(StoreError):
            store.create_edge(
                edge_id=str(uuid.uuid4()), type="association",
                relation="contradicts", from_node=asserter, to_node=url_id,
            )
        node = store.get_node(url_id)
        assert node["is_contested"] is False
        assert node["contested_at"] is None
        # No orphan edge or contestation event was created
        assert store._con.execute("SELECT COUNT(*) FROM edge").fetchone()[0] == 0
        assert store._con.execute("SELECT COUNT(*) FROM event_queue").fetchone()[0] == 0

    def test_url_node_in_list_nodes(self):
        store = _store()
        nid = _url_node(store)
        nodes = store.list_nodes(kind="url")
        assert len(nodes) == 1
        assert nodes[0]["id"] == nid
        assert nodes[0]["confidence"] is None


# ── Extracted nodes ────────────────────────────────────────────────────

class TestCreateExtractedNode:
    """AC2: kind='extracted' — tier/depth/content_path + provenance edge."""

    def test_extracted_node_stores_fields(self):
        store = _store()
        url_id = _url_node(store)
        nid = _extracted_node(store, url_id, fetcher_type="pdf",
                              content_path="/data/out.pdf.md")
        node = store.get_node(nid)
        assert node["kind"] == "extracted"
        assert node["tier"] == "extracted"
        assert node["depth"] == 1
        assert node["content_path"] == "/data/out.pdf.md"
        assert node["fetcher_type"] == "pdf"

    def test_extracted_node_creates_derived_from_edge_to_url(self):
        store = _store()
        url_id = _url_node(store)
        nid = _extracted_node(store, url_id)
        edges = store.list_edges(node_id=nid, type="provenance",
                                 relation="derived_from")
        assert len(edges) == 1
        assert edges[0]["from_node"] == nid
        assert edges[0]["to_node"] == url_id

    def test_find_derived_from_finds_extracted(self):
        store = _store()
        url_id = _url_node(store)
        nid = _extracted_node(store, url_id)
        found = store.find_derived_from(url_id)
        assert found is not None
        assert found["from_node"] == nid

    def test_extracted_node_requires_content_path(self):
        store = _store()
        url_id = _url_node(store)
        with pytest.raises(ValueError):
            store.create_node(
                node_id=str(uuid.uuid4()), kind="extracted", derived_from=url_id,
            )

    def test_extracted_node_requires_derived_from(self):
        store = _store()
        with pytest.raises(ValueError):
            store.create_node(
                node_id=str(uuid.uuid4()), kind="extracted",
                content_path="/tmp/x.md",
            )

    def test_extracted_node_requires_url_parent(self):
        store = _store()
        raw = str(uuid.uuid4())
        store.create_node(node_id=raw, kind="raw_source")
        with pytest.raises(ValueError):
            store.create_node(
                node_id=str(uuid.uuid4()), kind="extracted",
                content_path="/tmp/x.md", derived_from=raw,
            )

    def test_list_nodes_filters_by_new_kinds(self):
        store = _store()
        url_id = _url_node(store)
        _extracted_node(store, url_id)
        store.create_node(node_id=str(uuid.uuid4()), kind="raw_source")
        assert len(store.list_nodes(kind="url")) == 1
        assert len(store.list_nodes(kind="extracted")) == 1
        assert len(store.list_nodes()) == 3


# ── Source binding ─────────────────────────────────────────────────────

class TestSourceBinding:
    """AC3: source rows bind to URL-nodes; extracted nodes have no source row."""

    def test_source_binds_to_url_node(self):
        store = _store()
        url_id = _url_node(store)
        store.attach_source(
            node_id=url_id, canonical_key="https://example.com/a",
            source_url="https://example.com/a", title="A",
        )
        node = store.get_node(url_id)
        assert node["canonical_key"] == "https://example.com/a"
        assert node["source_url"] == "https://example.com/a"
        assert node["title"] == "A"

    def test_extracted_node_has_no_source_row(self):
        store = _store()
        url_id = _url_node(store)
        store.attach_source(
            node_id=url_id, canonical_key="https://example.com/a",
            source_url="https://example.com/a",
        )
        nid = _extracted_node(store, url_id)
        node = store.get_node(nid)
        assert node["canonical_key"] is None
        assert node["source_url"] is None
        assert node["title"] is None
        assert node["fetched_at"] is None

    def test_lookup_by_canonical_key_resolves_to_url_node(self):
        store = _store()
        url_id = _url_node(store)
        store.attach_source(
            node_id=url_id, canonical_key="https://example.com/a",
            source_url="https://example.com/a",
        )
        result = store.lookup_by_canonical_key("https://example.com/a")
        assert result == {"node_id": url_id, "failed": False}


# ── Confidence ─────────────────────────────────────────────────────────

class TestFetcherConfidence:
    """AC4: fetcher_type persisted; extracted confidence from fetcher map."""

    @pytest.mark.parametrize("fetcher_type,expected", [
        ("http", "medium"),
        ("youtube", "low"),
        ("pdf", "high"),
    ])
    def test_extracted_confidence_from_fetcher_type(self, fetcher_type, expected):
        store = _store()
        url_id = _url_node(store)
        nid = _extracted_node(store, url_id, fetcher_type=fetcher_type)
        node = store.get_node(nid)
        assert node["fetcher_type"] == fetcher_type
        assert node["confidence"] == expected
        assert store.compute_node_confidence(nid) == expected

    def test_extracted_node_with_unknown_fetcher_has_no_confidence(self):
        store = _store()
        url_id = _url_node(store)
        nid = _extracted_node(store, url_id, fetcher_type="rss")
        assert store.get_node(nid)["confidence"] is None
        assert store.compute_node_confidence(nid) is None

    def test_extracted_confidence_overridable_explicitly(self):
        store = _store()
        url_id = _url_node(store)
        nid = str(uuid.uuid4())
        store.create_node(
            node_id=nid, kind="extracted", fetcher_type="http",
            content_path="/tmp/e.md", derived_from=url_id, confidence="high",
        )
        assert store.get_node(nid)["confidence"] == "high"

    def test_fetcher_type_in_list_nodes(self):
        store = _store()
        url_id = _url_node(store)
        nid = _extracted_node(store, url_id, fetcher_type="pdf")
        nodes = store.list_nodes(kind="extracted")
        assert len(nodes) == 1
        assert nodes[0]["fetcher_type"] == "pdf"
        assert nodes[0]["id"] == nid

    def test_notes_and_raw_source_rules_unchanged(self):
        """C1/C2 still apply to raw_source/notes nodes."""
        store = _store()
        l0 = str(uuid.uuid4())
        store.create_node(node_id=l0, kind="raw_source")
        note = str(uuid.uuid4())
        store.create_node(node_id=note, kind="summary", tier="notes", depth=1)
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=note, to_node=l0)
        assert store.compute_node_confidence(l0) == "low"
        assert store.compute_node_confidence(note) == "medium"


# ── Contestation ───────────────────────────────────────────────────────

class TestContestation:
    """URL nodes can never become contested; extracted nodes can."""

    def test_extracted_node_can_be_contested(self):
        store = _store()
        url_id = _url_node(store)
        ext = _extracted_node(store, url_id)
        asserter = str(uuid.uuid4())
        store.create_node(node_id=asserter, kind="summary", depth=1)
        store.create_edge(edge_id=str(uuid.uuid4()), type="association",
                          relation="contradicts", from_node=asserter, to_node=ext)
        node = store.get_node(ext)
        assert node is not None
        assert node["is_contested"] is True
        assert node["contested_at"] is not None

    @pytest.mark.parametrize("fetcher_type,map_value", [
        ("http", "medium"),
        ("pdf", "high"),
    ])
    def test_contradicted_extracted_compute_returns_low(self, fetcher_type, map_value):
        """C4 (incoming contradicts) overrides the fetcher map for extracted
        nodes: stored and computed confidence both drop to low."""
        store = _store()
        url_id = _url_node(store)
        ext = _extracted_node(store, url_id, fetcher_type=fetcher_type)
        # Fetcher map governs before contestation
        assert store.compute_node_confidence(ext) == map_value
        asserter = str(uuid.uuid4())
        store.create_node(node_id=asserter, kind="summary", depth=1)
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="association",
            relation="contradicts", from_node=asserter, to_node=ext,
        )
        # Stored (written by _propagate_contradiction) and computed agree
        assert store.get_node(ext)["confidence"] == "low"
        assert store.compute_node_confidence(ext) == "low"

    def test_contradicting_extracted_does_not_contest_url_parent(self):
        store = _store()
        url_id = _url_node(store)
        ext = _extracted_node(store, url_id)
        asserter = str(uuid.uuid4())
        store.create_node(node_id=asserter, kind="summary", depth=1)
        store.create_edge(edge_id=str(uuid.uuid4()), type="association",
                          relation="contradicts", from_node=asserter, to_node=ext)
        url = store.get_node(url_id)
        assert url is not None
        assert url["is_contested"] is False
        assert url["contested_at"] is None
