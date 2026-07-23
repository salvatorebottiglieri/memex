"""Tests for confidence scoring per #47/#49.

Covers: schema migration, compute_node_confidence, create_node
confidence param, get_node/list_nodes exposure, CLI show/list emission,
derive confidence (medium), synthesize confidence (min of parents).
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from memex.store import Store

from tests.conftest import _run_memex, FAKE_FETCHER, ingest, _store, _utcnow

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"




# ── Schema ──────────────────────────────────────────────────────────────

class TestSchema:
    def test_confidence_column_exists(self):
        """Schema adds confidence TEXT column with CHECK constraint."""
        store = _store()
        cols = {
            r[1] for r in store._con.execute("PRAGMA table_info(node)").fetchall()
        }
        assert "confidence" in cols

    def test_confidence_check_constraint(self):
        """Only 'high', 'medium', 'low' are accepted."""
        store = _store()
        nid = str(uuid.uuid4())
        store._con.execute(
            "INSERT INTO node (id, kind, trust_state, depth, content_path, created_at, confidence) "
            "VALUES (?, 'raw_source', 'draft', 0, '', ?, 'low')",
            (nid, _utcnow()),
        )
        # invalid value should fail
        import pytest
        with pytest.raises(sqlite3.IntegrityError):
            store._con.execute(
                "UPDATE node SET confidence = 'invalid' WHERE id = ?", (nid,)
            )

    def test_init_schema_idempotent_with_confidence(self):
        """Adding confidence column is idempotent."""
        store = _store()
        store.init_schema()  # second call must not crash


# ── Backfill ────────────────────────────────────────────────────────────

class TestBackfill:
    def _create_node(self, store, *, tier=None, kind="raw_source", depth=0):
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind=kind, tier=tier, depth=depth)
        return nid

    def test_backfill_l0_node_gets_low(self):
        """L0 node (no tier) gets confidence='low'."""
        store = _store()
        # Manually insert without confidence to simulate pre-migration node
        nid = str(uuid.uuid4())
        store._con.execute(
            "INSERT INTO node (id, kind, trust_state, depth, content_path, created_at) "
            "VALUES (?, 'raw_source', 'draft', 0, '', ?)",
            (nid, _utcnow()),
        )
        # Run backfill
        store._backfill_confidence()
        row = store._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (nid,)
        ).fetchone()
        assert row["confidence"] == "low"

    def test_backfill_notes_node_gets_medium(self):
        """Notes-tier node gets confidence='medium'."""
        store = _store()
        nid = str(uuid.uuid4())
        store._con.execute(
            "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at) "
            "VALUES (?, 'summary', 'notes', 'draft', 1, '', ?)",
            (nid, _utcnow()),
        )
        store._backfill_confidence()
        row = store._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (nid,)
        ).fetchone()
        assert row["confidence"] == "medium"


# ── compute_node_confidence ─────────────────────────────────────────────

class TestComputeNodeConfidence:
    def test_l0_node_confidence_is_low(self):
        """Node with no incoming provenance edges → low."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0)
        assert store.compute_node_confidence(nid) == "low"

    def test_one_parent_confidence_is_medium(self):
        """Node with 1 incoming derived_from → medium."""
        store = _store()
        parent = str(uuid.uuid4())
        child = str(uuid.uuid4())
        for nid in (parent, child):
            store.create_node(node_id=nid, kind="summary", depth=1)
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance",
            relation="derived_from", from_node=child, to_node=parent,
        )
        assert store.compute_node_confidence(child) == "medium"

    def test_two_parents_confidence_is_high(self):
        """Node with 2+ incoming derived_from and no contradicts → high."""
        store = _store()
        p1 = str(uuid.uuid4())
        p2 = str(uuid.uuid4())
        child = str(uuid.uuid4())
        for nid in (p1, p2, child):
            store.create_node(node_id=nid, kind="summary", depth=1)
        for parent in (p1, p2):
            store.create_edge(
                edge_id=str(uuid.uuid4()), type="provenance",
                relation="derived_from", from_node=child, to_node=parent,
            )
        assert store.compute_node_confidence(child) == "high"

    def test_contradicts_edge_confidence_is_low(self):
        """Node with any incoming contradicts → low regardless of parents."""
        store = _store()
        parent = str(uuid.uuid4())
        child = str(uuid.uuid4())
        contradictor = str(uuid.uuid4())
        for nid in (parent, child, contradictor):
            store.create_node(node_id=nid, kind="summary", depth=1)
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance",
            relation="derived_from", from_node=child, to_node=parent,
        )
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="association",
            relation="contradicts", from_node=contradictor, to_node=child,
        )
        assert store.compute_node_confidence(child) == "low"

    def test_contradicts_is_incoming_not_outgoing(self):
        """Only incoming contradicts edges affect confidence (outgoing does not)."""
        store = _store()
        parent = str(uuid.uuid4())
        child = str(uuid.uuid4())
        other = str(uuid.uuid4())
        for nid in (parent, child, other):
            store.create_node(node_id=nid, kind="summary", depth=1)
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance",
            relation="derived_from", from_node=child, to_node=parent,
        )
        # Outgoing contradicts — child contradicts someone else, not incoming
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="association",
            relation="contradicts", from_node=child, to_node=other,
        )
        assert store.compute_node_confidence(child) == "medium"

    def test_unknown_node_raises(self):
        """compute_node_confidence on non-existent node raises."""
        store = _store()
        import pytest
        with pytest.raises(ValueError, match="not found"):
            store.compute_node_confidence("nonexistent")


# ── create_node ─────────────────────────────────────────────────────────

class TestCreateNode:
    def test_create_node_with_confidence(self):
        """Explicit confidence parameter is stored."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0,
                          confidence="high")
        row = store._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (nid,)
        ).fetchone()
        assert row["confidence"] == "high"

    def test_create_node_without_confidence_computes_low(self):
        """Omitting confidence auto-computes: L0 → low."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0)
        row = store._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (nid,)
        ).fetchone()
        assert row["confidence"] == "low"


# ── get_node ────────────────────────────────────────────────────────────

class TestGetNode:
    def test_get_node_includes_confidence(self):
        """get_node returns confidence in the dict."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0,
                          confidence="low")
        node = store.get_node(nid)
        assert node is not None
        assert node["confidence"] == "low"

    def test_get_node_confidence_is_high(self):
        """Explicit confidence='high' is returned via get_node."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="summary", depth=1,
                          confidence="high")
        node = store.get_node(nid)
        assert node["confidence"] == "high"


# ── list_nodes ──────────────────────────────────────────────────────────

class TestListNodes:
    def test_list_nodes_includes_confidence(self):
        """list_nodes returns confidence in each node dict."""
        store = _store()
        nid = str(uuid.uuid4())
        store.create_node(node_id=nid, kind="raw_source", depth=0,
                          confidence="low")
        nodes = store.list_nodes()
        assert len(nodes) == 1
        assert nodes[0]["confidence"] == "low"


# ── CLI show ────────────────────────────────────────────────────────────

class TestShow:
    def test_show_emits_confidence(self, store):
        result = ingest(store, "https://example.com/article")
        ingested = json.loads(result.stdout)
        show_result = _run_memex(
            ["show", "--db", str(store["db"]), "--vault", str(store["vault"]),
             ingested["id"]],
        )
        assert show_result.returncode == 0, show_result.stderr
        data = json.loads(show_result.stdout)
        assert "confidence" in data
        assert data["confidence"] == "low"


# ── CLI list ────────────────────────────────────────────────────────────

class TestList:
    def test_list_emits_confidence(self, store):
        ingest(store, "https://example.com/article")
        list_result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert list_result.returncode == 0, list_result.stderr
        data = json.loads(list_result.stdout)
        assert len(data) >= 1
        assert "confidence" in data[0]


# ── Derive confidence ───────────────────────────────────────────────────

class TestDeriveConfidence:
    def _ingest(self, store, url: str) -> dict:
        result = _run_memex(
            ["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
            env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def _derive(self, store, node_id: str):
        return _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )

    def test_derive_confidence_is_medium(self, store):
        """After derive (1 parent), confidence is medium."""
        ingested = self._ingest(store, "https://example.com/article")
        result = self._derive(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        deriv_id = data["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT confidence FROM node WHERE id = ?", (deriv_id,)
        ).fetchone()
        con.close()
        assert row is not None
        assert row is not None
        assert row[0] == "medium"


# ── Synthesize confidence ───────────────────────────────────────────────

class TestSynthesizeConfidence:
    def _ingest(self, store, url: str) -> dict:
        result = _run_memex(
            ["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
            env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def _derive(self, store, node_id: str):
        return _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )

    def _synthesize(self, store, *node_ids: str):
        return _run_memex(
            ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]),
             *node_ids],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )

    def test_synthesize_confidence_is_medium_from_medium_parents(self, store):
        """Synthesize from two notes-tier (medium) parents → medium."""
        a = self._ingest(store, "https://example.com/article-a")
        b = self._ingest(store, "https://example.com/article-b")
        da = self._derive(store, a["id"])
        db = self._derive(store, b["id"])
        assert da.returncode == 0
        assert db.returncode == 0

        result = self._synthesize(store, json.loads(da.stdout)["id"],
                                   json.loads(db.stdout)["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        syn_id = data["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn_id,)
        ).fetchone()
        con.close()
        assert row is not None
        # min(medium, medium) = medium
        assert row is not None
        # min(medium, medium) = medium
        assert row[0] == "medium"

    def test_synthesize_confidence_from_low_parent(self, store):
        """Synthesize from medium + low → low (min)."""
        a = self._ingest(store, "https://example.com/article-a")
        b = self._ingest(store, "https://example.com/article-b")
        da = self._derive(store, a["id"])
        assert da.returncode == 0
        # Use an L0 (low confidence) as second parent
        result = self._synthesize(store, json.loads(da.stdout)["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        syn_id = data["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn_id,)
        ).fetchone()
        con.close()
        assert row is not None
        # min(medium, low) = low
        assert row[0] == "low"
# ── Contradicts cascade ────────────────────────────────────────────

class TestContradictsCascade:
    """Confidence cascade when a contradicts edge is written."""

    def test_contradicts_target_confidence_low(self):
        """Contradicts edge sets target node confidence to low."""
        s = _store()
        a = str(uuid.uuid4())
        b = str(uuid.uuid4())
        note1 = str(uuid.uuid4())
        note2 = str(uuid.uuid4())
        s.create_node(node_id=a, kind="raw_source")
        s.create_node(node_id=b, kind="raw_source")
        s.create_node(node_id=note1, kind="note", tier="notes")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=note1, to_node=a,
        )
        # Note with 1 parent → medium
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (note1,))
        s.create_node(node_id=note2, kind="note", tier="notes")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=note2, to_node=b,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (note2,))

        # Both notes start medium (1 parent each)
        for nid in (note1, note2):
            row = s._con.execute(
                "SELECT confidence FROM node WHERE id = ?", (nid,)
            ).fetchone()
            assert row[0] == "medium", f"expected medium for {nid}, got {row[0]}"

        # Contradicts: note1 → note2 (target = note2)
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="association", relation="contradicts",
            from_node=note1, to_node=note2,
        )

        # Target note2 should be low
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (note2,)
        ).fetchone()
        assert row[0] == "low", f"expected low for note2, got {row[0]}"

        # Unaffected note1 stays medium
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (note1,)
        ).fetchone()
        assert row[0] == "medium", f"expected medium for note1, got {row[0]}"

    def test_contradicts_cascade_synthesis(self):
        """Contradicts target → low, then descendant synthesis recomputed."""
        s = _store()
        a = str(uuid.uuid4())
        b = str(uuid.uuid4())
        note1 = str(uuid.uuid4())
        note2 = str(uuid.uuid4())
        syn = str(uuid.uuid4())

        s.create_node(node_id=a, kind="raw_source")
        s.create_node(node_id=b, kind="raw_source")
        s.create_node(node_id=note1, kind="note", tier="notes")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=note1, to_node=a,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (note1,))
        s.create_node(node_id=note2, kind="note", tier="notes")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=note2, to_node=b,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (note2,))

        # Synthesis from note1 + note2 → medium (min of medium, medium)
        s.create_node(node_id=syn, kind="synthesis", tier="synthesis")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=syn, to_node=note1,
        )
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=syn, to_node=note2,
        )
        # Min of parent confidences for synthesis
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (syn,))

        # Synthesis should be medium (min of medium, medium)
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn,)
        ).fetchone()
        assert row[0] == "medium", f"expected medium for syn, got {row[0]}"

        # Contradicts: note1 → note2 (target = note2)
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="association", relation="contradicts",
            from_node=note1, to_node=note2,
        )

        # Target note2 goes low
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (note2,)
        ).fetchone()
        assert row[0] == "low"

        # Synthesis descendant should cascade to low (min of medium, low)
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn,)
        ).fetchone()
        assert row[0] == "low", f"expected low for syn after cascade, got {row[0]}"

    def test_contradicts_cascade_transitive(self):
        """Transitive cascade: grandchild synthesis also recomputed."""
        s = _store()
        a = str(uuid.uuid4())
        b = str(uuid.uuid4())
        note1 = str(uuid.uuid4())
        note2 = str(uuid.uuid4())
        syn1 = str(uuid.uuid4())
        syn2 = str(uuid.uuid4())
        note3 = str(uuid.uuid4())

        # Two L0 roots
        s.create_node(node_id=a, kind="raw_source")
        s.create_node(node_id=b, kind="raw_source")

        # Two notes
        s.create_node(node_id=note1, kind="note", tier="notes")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=note1, to_node=a,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (note1,))
        s.create_node(node_id=note2, kind="note", tier="notes")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=note2, to_node=b,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (note2,))

        # First synthesis: syn1 from note1 + note2 → medium
        s.create_node(node_id=syn1, kind="synthesis", tier="synthesis")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=syn1, to_node=note1,
        )
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=syn1, to_node=note2,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (syn1,))

        # A third note
        s.create_node(node_id=note3, kind="note", tier="notes")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=note3, to_node=a,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (note3,))

        # Second synthesis: syn2 from syn1 + note3 → medium
        s.create_node(node_id=syn2, kind="synthesis", tier="synthesis")
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=syn2, to_node=syn1,
        )
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=syn2, to_node=note3,
        )
        s._con.execute("UPDATE node SET confidence = 'medium' WHERE id = ?", (syn2,))

        # Verify starting confidences
        for nid in (note1, note2, note3):
            row = s._con.execute(
                "SELECT confidence FROM node WHERE id = ?", (nid,)
            ).fetchone()
            assert row[0] == "medium", f"expected medium for {nid}, got {row[0]}"
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn1,)
        ).fetchone()
        assert row[0] == "medium", f"expected medium for syn1, got {row[0]}"
        # syn2: min(syn1=medium, note3=medium) = medium
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn2,)
        ).fetchone()
        assert row[0] == "medium", f"expected medium for syn2, got {row[0]}"

        # Contradicts: note1 → note2 (target = note2)
        s.create_edge(
            edge_id=str(uuid.uuid4()), type="association", relation="contradicts",
            from_node=note1, to_node=note2,
        )

        # note2 becomes low
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (note2,)
        ).fetchone()
        assert row[0] == "low"

        # syn1: descendant of note2, min(note1=medium, note2=low) = low
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn1,)
        ).fetchone()
        assert row[0] == "low", f"expected low for syn1, got {row[0]}"

        # syn2: descendant of syn1, min(syn1=low, note3=medium) = low
        row = s._con.execute(
            "SELECT confidence FROM node WHERE id = ?", (syn2,)
        ).fetchone()
        assert row[0] == "low", f"expected low for syn2, got {row[0]}"

