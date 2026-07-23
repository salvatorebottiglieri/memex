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



def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

def _setup_db(tmp_path: Path) -> tuple[sqlite3.Connection, str, Path]:
    """Create a minimal db with one L0 node and one derivation node + edge."""
    from memex.store import Store

    db_path = tmp_path / "memex.db"
    with Store.open(db_path) as store:
        store.init_schema()
        l0_id = str(uuid.uuid4())
        store.create_node(node_id=l0_id, kind="raw_source", trust_state="draft", depth=0,
                          content_path="", created_at=_utcnow())
        store.attach_source(node_id=l0_id, canonical_key="test://l0",
                            source_url="https://test.example/l0", fetched_at=_utcnow())

        deriv_id = str(uuid.uuid4())
        store.create_node(node_id=deriv_id, kind="summary", tier="notes",
                          trust_state="draft", depth=1, content_path="", created_at=_utcnow())
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
                          from_node=deriv_id, to_node=l0_id)

        content_path = tmp_path / f"{deriv_id}.md"
        content_path.write_text(VALID_CONTENT, encoding="utf-8")
        store._con.execute("UPDATE node SET content_path = ? WHERE id = ?",
                           (str(content_path), deriv_id))

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
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

    def test_synthesis_statements_column_passes_even_without_marker(self, tmp_path):
        """A derivation whose synthesis_statements column is populated passes the
        synthesis check even when the markdown has no '> Synthesis:' marker."""
        import json as _json
        con, deriv_id, content_path = _setup_db(tmp_path)
        # Overwrite content: drop the marker
        content_without_marker = "This derivation is long enough but has no synthesis marker. " * 5
        content_path.write_text(content_without_marker, encoding="utf-8")
        # Persist the structured statements
        con.execute(
            "UPDATE node SET synthesis_statements = ? WHERE id = ?",
            (_json.dumps(["Inference A.", "Inference B."]), deriv_id),
        )
        con.commit()

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
# Check 5: Tier / depth consistency
# ---------------------------------------------------------------------------

class TestTierDepthConsistency:
    def test_notes_tier_with_wrong_depth_fails(self, tmp_path):
        """A node with tier=notes but depth=0 fails tier/depth check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        con.execute("UPDATE node SET depth = 0 WHERE id = ?", (deriv_id,))
        con.commit()
        result = run_checks(con, deriv_id, content_path)
        con.close()
        assert not result.passed
        assert any("Tier/depth" in f for f in result.failures)

    def test_notes_tier_depth_1_passes(self, tmp_path):
        """A node with tier=notes and depth=1 passes tier/depth check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        # deriv already has tier='notes', depth=1
        result = run_checks(con, deriv_id, content_path)
        con.close()
        assert result.passed is True

    def test_synthesis_tier_depth_2_passes(self, tmp_path):
        """A node with tier=synthesis and depth=2 passes tier/depth check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        con.execute("UPDATE node SET tier = 'synthesis', depth = 2 WHERE id = ?", (deriv_id,))
        con.commit()
        result = run_checks(con, deriv_id, content_path)
        con.close()
        assert result.passed is True

    def test_synthesis_tier_depth_1_fails(self, tmp_path):
        """A node with tier=synthesis but depth=1 fails tier/depth check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        con.execute("UPDATE node SET tier = 'synthesis' WHERE id = ?", (deriv_id,))
        con.commit()
        result = run_checks(con, deriv_id, content_path)
        con.close()
        assert not result.passed
        assert any("Tier/depth" in f for f in result.failures)

    def test_null_tier_depth_0_passes(self, tmp_path):
        """A raw_source (NULL tier) with depth=0 passes tier/depth check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        l0_id = con.execute("SELECT id FROM node WHERE kind = 'raw_source'").fetchone()[0]
        # L0 has tier=NULL, depth=0 — run check on it
        result = run_checks(con, l0_id, content_path)
        con.close()
        # Tier check should not produce any failure even if other checks fail
        assert not any("Tier/depth" in f for f in result.failures)

    def test_null_tier_depth_1_fails(self, tmp_path):
        """A node with NULL tier but depth=1 fails tier/depth check."""
        con, deriv_id, content_path = _setup_db(tmp_path)
        con.execute("UPDATE node SET tier = NULL, depth = 1 WHERE id = ?", (deriv_id,))
        con.commit()
        result = run_checks(con, deriv_id, content_path)
        con.close()
        assert not result.passed
        assert any("Tier/depth" in f for f in result.failures)

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
