"""Tests for the deterministic Checks module (src/memex/checks.py).

Checks are pure/deterministic — no LLM, no network, no randomness.
Tests use fixture derivations (passing and failing variants).
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from memex.checks import CheckResult, run_checks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_CONTENT = (
    "This is a derivation with enough content to pass the size check.\n\n"
    "> Synthesis: The author implies a broader pattern beyond what is stated directly.\n\n"
    "The source material covers the subject thoroughly. " * 5
)


def _setup_db(tmp_path: Path) -> tuple[sqlite3.Connection, str, Path]:
    """Create a minimal db with one L0 node and one derivation node + edge."""
    db_path = tmp_path / "memex.db"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS node (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            tier         TEXT,
            trust_state  TEXT NOT NULL CHECK (trust_state IN ('draft','auto-verified','human-approved','stale')),
            depth        INTEGER NOT NULL,
            content_path TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            check_failures TEXT,
            is_contested INTEGER NOT NULL DEFAULT 0,
            contested_at TEXT
        );

        CREATE TABLE IF NOT EXISTS source (
            node_id       TEXT PRIMARY KEY REFERENCES node(id),
            canonical_key TEXT NOT NULL UNIQUE,
            source_url    TEXT NOT NULL,
            title         TEXT,
            fetched_at    TEXT,
            failed        INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS edge (
            id        TEXT PRIMARY KEY,
            type      TEXT NOT NULL CHECK (type IN ('provenance','association')),
            relation  TEXT NOT NULL CHECK (relation IN ('derived_from','related','contradicts','refines')),
            from_node TEXT NOT NULL REFERENCES node(id),
            to_node   TEXT NOT NULL REFERENCES node(id),
            written_by TEXT NOT NULL DEFAULT 'human'
        );

        CREATE TABLE IF NOT EXISTS event_queue (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type     TEXT NOT NULL CHECK (event_type IN ('contradicts_edge_needs_review')),
            edge_id        TEXT NOT NULL REFERENCES edge(id),
            target_node_id TEXT NOT NULL REFERENCES node(id),
            created_at     TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN ('pending','closed')) DEFAULT 'pending',
            closed_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_event_queue_status ON event_queue(status);
        CREATE INDEX IF NOT EXISTS idx_event_queue_target ON event_queue(target_node_id);

        CREATE TABLE IF NOT EXISTS event_node_link (
            event_id     INTEGER NOT NULL REFERENCES event_queue(id),
            node_id      TEXT NOT NULL REFERENCES node(id),
            contested_at TEXT NOT NULL,
            PRIMARY KEY (event_id, node_id)
        );
        CREATE INDEX IF NOT EXISTS idx_event_node_link_node ON event_node_link(node_id);
        CREATE INDEX IF NOT EXISTS idx_event_node_link_event ON event_node_link(event_id);

        CREATE TABLE IF NOT EXISTS review_proposal (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id              INTEGER NOT NULL UNIQUE REFERENCES event_queue(id),
            affected_node_ids     TEXT NOT NULL,
            damage_boundary_node_id TEXT REFERENCES node(id),
            rationale_md          TEXT NOT NULL,
            confidence            TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
            status                TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected','dismissed')) DEFAULT 'pending',
            human_note            TEXT,
            created_at            TEXT NOT NULL,
            resolved_at           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_review_proposal_status ON review_proposal(status);
        """
    )

    l0_id = str(uuid.uuid4())
    deriv_id = str(uuid.uuid4())
    edge_id = str(uuid.uuid4())

    # Insert L0 node (raw_source)
    con.execute(
        "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at) VALUES (?, 'raw_source', NULL, 'draft', 0, '', '2024-01-01')",
        (l0_id,),
    )

    # Write content file
    content_path = tmp_path / f"{deriv_id}.md"
    content_path.write_text(VALID_CONTENT, encoding="utf-8")

    # Insert derivation node
    con.execute(
        "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at) VALUES (?, 'summary', 'notes', 'draft', 1, ?, '2024-01-01')",
        (deriv_id, str(content_path)),
    )

    # Insert provenance edge: deriv_id --derived_from--> l0_id
    con.execute(
        "INSERT INTO edge (id, type, relation, from_node, to_node) VALUES (?, 'provenance', 'derived_from', ?, ?)",
        (edge_id, deriv_id, l0_id),
    )
    con.commit()

    return con, deriv_id, content_path


# ---------------------------------------------------------------------------
# Tracer bullet: all-passing check
# ---------------------------------------------------------------------------

class TestRunChecksAllPass:
    def test_all_checks_pass_returns_passed_true(self, tmp_path):
        """Tracer bullet: a valid derivation yields CheckResult(passed=True, failures=[])."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert isinstance(result, CheckResult)
        assert result.passed is True
        assert result.failures == []

    def test_check_result_is_dataclass_like(self, tmp_path):
        """CheckResult exposes .passed and .failures attributes."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert hasattr(result, "passed")
        assert hasattr(result, "failures")


# ---------------------------------------------------------------------------
# Check 1: Provenance edge resolves
# ---------------------------------------------------------------------------

class TestProvenanceCheck:
    def test_missing_provenance_edge_fails(self, tmp_path):
        """A derivation with no derived_from edge fails the provenance check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        # Remove the edge
        con.execute("DELETE FROM edge WHERE from_node = ?", (deriv_id,))
        con.commit()

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert any("provenance" in f.lower() for f in result.failures)

    def test_edge_pointing_to_nonexistent_node_fails(self, tmp_path):
        """A derived_from edge to a non-existent node fails the provenance check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        ghost_id = str(uuid.uuid4())

        # Replace edge target with a ghost node id (must bypass FK since FK may not cascade)
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("UPDATE edge SET to_node = ? WHERE from_node = ?", (ghost_id, deriv_id))
        con.execute("PRAGMA foreign_keys = ON")
        con.commit()

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert any("provenance" in f.lower() for f in result.failures)


# ---------------------------------------------------------------------------
# Check 2: No dangling references (provenance target is L0 / raw_source)
# ---------------------------------------------------------------------------

class TestDanglingRefCheck:
    def test_provenance_target_is_not_raw_source_fails(self, tmp_path):
        """A derivation whose provenance target is not kind=raw_source fails the dangling-ref check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        # Change the l0 node's kind to something other than raw_source
        con.execute(
            "UPDATE node SET kind = 'summary' WHERE kind = 'raw_source'",
        )
        con.commit()

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert any("dangling" in f.lower() or "raw_source" in f.lower() or "l0" in f.lower() for f in result.failures)


# ---------------------------------------------------------------------------
# Check 3: > Synthesis: marker present
# ---------------------------------------------------------------------------

class TestSynthesisMarkerCheck:
    def test_missing_synthesis_marker_fails(self, tmp_path):
        """A derivation without "> Synthesis:" stays draft and flags the check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        # Overwrite content without the synthesis marker
        content_without_synthesis = "This derivation is long enough but has no synthesis marker. " * 5
        content_path.write_text(content_without_synthesis, encoding="utf-8")

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert any("synthesis" in f.lower() for f in result.failures)

    def test_synthesis_marker_present_passes_this_check(self, tmp_path):
        """A derivation with "> Synthesis:" passes the synthesis check (other checks OK too)."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        # VALID_CONTENT already has the marker
        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is True


# ---------------------------------------------------------------------------
# Check 4: Size / scope bounds
# ---------------------------------------------------------------------------

class TestSizeBoundsCheck:
    def test_empty_content_fails(self, tmp_path):
        """An empty derivation fails the size check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        content_path.write_text("", encoding="utf-8")

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert any("size" in f.lower() or "length" in f.lower() or "short" in f.lower() for f in result.failures)

    def test_too_short_content_fails(self, tmp_path):
        """A derivation shorter than MIN_CHARS fails the size check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        # Write something with a synthesis marker but too short
        content_path.write_text("> Synthesis: short", encoding="utf-8")

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert any("size" in f.lower() or "length" in f.lower() or "short" in f.lower() for f in result.failures)

    def test_too_long_content_fails(self, tmp_path):
        """A derivation longer than MAX_CHARS fails the size check."""
        from memex.checks import MAX_CHARS
        con, deriv_id, content_path = _setup_db(tmp_path)
        # Write content that exceeds MAX_CHARS with a synthesis marker
        long_content = "> Synthesis: too long\n" + "x" * (MAX_CHARS + 1)
        content_path.write_text(long_content, encoding="utf-8")

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert any("size" in f.lower() or "length" in f.lower() or "long" in f.lower() for f in result.failures)

    def test_within_bounds_passes_size_check(self, tmp_path):
        """VALID_CONTENT (within bounds) passes the size check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is True


# ---------------------------------------------------------------------------
# Multiple failures accumulate
# ---------------------------------------------------------------------------

class TestMultipleFailures:
    def test_multiple_failures_all_reported(self, tmp_path):
        """When several checks fail, all failures are collected (not fail-fast)."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        # Remove edge AND strip synthesis marker AND make content too short
        con.execute("DELETE FROM edge WHERE from_node = ?", (deriv_id,))
        con.commit()
        content_path.write_text("No synthesis here.", encoding="utf-8")

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        # Expect at least 2 failures reported (provenance + synthesis + size)
        assert len(result.failures) >= 2

    def test_checks_are_deterministic(self, tmp_path):
        """Running checks twice on the same input yields identical results."""
        con, deriv_id, content_path = _setup_db(tmp_path)

        result1 = run_checks(con, deriv_id, content_path)
        result2 = run_checks(con, deriv_id, content_path)
        con.close()

        assert result1.passed == result2.passed
        assert result1.failures == result2.failures
