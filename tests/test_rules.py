"""Tests for the declarative rule engine (src/memex/rules.py).

TDD: red-green-refactor.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from memex.rules import CONFIDENCE_RULES, CHECK_RULES, MIN_CHARS, MAX_CHARS, Rule
from memex.store import Store
from memex.checks import CheckResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_CONTENT = (
    "This is a derivation with enough content to pass the size check.\n\n"
    "> Synthesis: The author implies a broader pattern beyond what is stated directly.\n\n"
    "The source material covers the subject thoroughly in great detail. "
    "This is additional text to reach the minimum character count for verification checks."
)


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _store() -> tuple[Store, sqlite3.Connection]:
    """Create an in-memory Store, return (store, con)."""
    con = sqlite3.connect(":memory:")
    store = Store(con)
    store.init_schema()
    return store, con


def _add_l0(store: Store) -> str:
    """Add an L0 node (legacy depth-0 shape — the rules are kind-agnostic),
    return its id."""
    l0_id = str(uuid.uuid4())
    store.create_node(node_id=l0_id, kind="raw_source", trust_state="draft", depth=0,
                      content_path="", created_at=_utcnow())
    store.attach_source(node_id=l0_id, canonical_key=f"test://{l0_id}",
                        source_url=f"https://test.example/{l0_id}", fetched_at=_utcnow())
    return l0_id


def _add_derived(store: Store, l0_id: str) -> str:
    """Create a notes derivation off l0_id, return its node id."""
    deriv_id = str(uuid.uuid4())
    store.create_node(node_id=deriv_id, kind="summary", tier="notes",
                      trust_state="draft", depth=1, content_path="", created_at=_utcnow())
    store.create_edge(edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
                      from_node=deriv_id, to_node=l0_id)
    return deriv_id


def _rule(rules, rid):
    return next(r for r in rules if r.id == rid)


def _setup_check_db(tmp_path: Path) -> tuple[sqlite3.Connection, str, Path]:
    """Create a minimal db with one L0 node and one derivation node + edge, plus valid content."""
    from memex.store import Store as S

    db_path = tmp_path / "memex.db"
    with S.open(db_path) as store:
        store.init_schema()
        l0_id = str(uuid.uuid4())
        # Legacy L0 shape (NULL tier, depth 0) — D5's tier/depth rule is
        # kind-agnostic, so the legacy kind works as a generic L0 here.
        store.create_node(node_id=l0_id, kind="raw_source", trust_state="draft", depth=0,
                          content_path="", created_at=_utcnow())
        store.attach_source(node_id=l0_id, canonical_key=f"test://{l0_id}",
                            source_url=f"https://test.example/{l0_id}", fetched_at=_utcnow())

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
# Rule dataclass
# ---------------------------------------------------------------------------

class TestRuleDataclass:
    def test_instantiation(self):
        rule = Rule(
            id="C1",
            category="confidence",
            description="Base confidence for L0 nodes",
            condition=lambda store, node_id: True,
            consequence="low",
        )
        assert rule.id == "C1"
        assert rule.category == "confidence"
        assert rule.consequence == "low"

    def test_frozen(self):
        rule = Rule(id="C1", category="confidence", description="x",
                    condition=lambda s, n: True, consequence="low")
        with pytest.raises(AttributeError):
            rule.id = "C2"  # type: ignore[misc]

    def test_hashable(self):
        rule = Rule(id="C1", category="confidence", description="x",
                    condition=lambda s, n: True, consequence="low")
        s = {rule}
        assert rule in s

    def test_condition_is_callable(self):
        rule = Rule(id="C1", category="confidence", description="x",
                    condition=lambda store, node_id: True, consequence="low")
        assert callable(rule.condition)


# ---------------------------------------------------------------------------
# Confidence rules — conditions
# ---------------------------------------------------------------------------

class TestConfidenceRuleC4:
    """C4: Contradiction overrides — ANY incoming contradicts -> low."""

    def test_incoming_contradict_sets_low(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        store.create_edge(edge_id=str(uuid.uuid4()), type="association",
                          relation="contradicts", from_node=l0_id, to_node=deriv_id)

        rule = _rule(CONFIDENCE_RULES, "C4")
        assert rule.condition(store, deriv_id) is True

    def test_no_contradict_does_not_fire(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)

        rule = _rule(CONFIDENCE_RULES, "C4")
        assert rule.condition(store, deriv_id) is False

    def test_fires_for_extracted_node(self):
        """C4 is kind-agnostic: it fires on extracted nodes too, so
        compute_node_confidence can honour it before the fetcher map."""
        store, _ = _store()
        url_id = str(uuid.uuid4())
        store.create_node(node_id=url_id, kind="url")
        ext = str(uuid.uuid4())
        store.create_node(
            node_id=ext, kind="extracted", fetcher_type="http",
            content_path="/tmp/e.md", derived_from=url_id,
        )
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="association",
            relation="contradicts", from_node=url_id, to_node=ext,
        )

        rule = _rule(CONFIDENCE_RULES, "C4")
        assert rule.condition(store, ext) is True


class TestConfidenceRuleC3:
    """C3: 2+ provenance parents -> high."""

    def test_two_parents_returns_high(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        l0_2 = _add_l0(store)
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=deriv_id, to_node=l0_2)

        rule = _rule(CONFIDENCE_RULES, "C3")
        assert rule.condition(store, deriv_id) is True

    def test_one_parent_does_not_fire_c3(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)

        rule = _rule(CONFIDENCE_RULES, "C3")
        assert rule.condition(store, deriv_id) is False


class TestConfidenceRuleC2:
    """C2: 1 provenance parent -> medium."""

    def test_one_parent_returns_medium(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)

        rule = _rule(CONFIDENCE_RULES, "C2")
        assert rule.condition(store, deriv_id) is True

    def test_two_parents_does_not_fire_c2(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        l0_2 = _add_l0(store)
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=deriv_id, to_node=l0_2)

        rule = _rule(CONFIDENCE_RULES, "C2")
        assert rule.condition(store, deriv_id) is False


class TestConfidenceRuleC1:
    """C1: 0 parents -> low."""

    def test_l0_node_fires_c1(self):
        store, _ = _store()
        l0_id = _add_l0(store)

        rule = _rule(CONFIDENCE_RULES, "C1")
        assert rule.condition(store, l0_id) is True

    def test_derived_node_does_not_fire_c1(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)

        rule = _rule(CONFIDENCE_RULES, "C1")
        assert rule.condition(store, deriv_id) is False


class TestConfidenceRulePriority:
    """Rules fire in priority order: C4 > C3 > C2 > C1. First match wins."""

    def test_contradict_overrides_high_parent_count(self):
        """C4 fires before C3 even with 2+ parents."""
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        l0_2 = _add_l0(store)
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=deriv_id, to_node=l0_2)
        store.create_edge(edge_id=str(uuid.uuid4()), type="association",
                          relation="contradicts", from_node=l0_id, to_node=deriv_id)

        result = None
        for r in CONFIDENCE_RULES:
            if r.condition(store, deriv_id):
                result = r.consequence
                break
        assert result == "low"

    def test_c3_matches_before_c2(self):
        """C3 fires before C2 when 2+ parents."""
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        l0_2 = _add_l0(store)
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=deriv_id, to_node=l0_2)

        result = None
        for r in CONFIDENCE_RULES:
            if r.condition(store, deriv_id):
                result = r.consequence
                break
        assert result == "high"


# ---------------------------------------------------------------------------
# Check rules — conditions
# ---------------------------------------------------------------------------

class TestCheckRuleD1:
    """D1: Provenance edge exists."""

    def test_missing_edge_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("DELETE FROM edge WHERE from_node = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D1")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1
        assert any("provenance" in f.lower() for f in failures)

    def test_edge_to_nonexistent_target_fails(self, tmp_path):
        """Dangling reference — D2 catches what D1's edge-existence check skips."""
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        ghost_id = str(uuid.uuid4())
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("UPDATE edge SET to_node = ? WHERE from_node = ?", (ghost_id, deriv_id))
        con.execute("PRAGMA foreign_keys = ON")
        con.commit()

        rule = _rule(CHECK_RULES, "D2")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1

    def test_valid_edge_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)

        rule = _rule(CHECK_RULES, "D1")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert failures == []


class TestCheckRuleD2:
    """D2: Provenance target nodes exist."""

    def test_dangling_ref_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        ghost_id = str(uuid.uuid4())
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("UPDATE edge SET to_node = ? WHERE from_node = ?", (ghost_id, deriv_id))
        con.execute("PRAGMA foreign_keys = ON")
        con.commit()

        rule = _rule(CHECK_RULES, "D2")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1
        assert any("dangling" in f.lower() for f in failures)

    def test_valid_target_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)

        rule = _rule(CHECK_RULES, "D2")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert failures == []


class TestCheckRuleD3:
    """D3: Synthesis statement consistency."""

    def test_missing_marker_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        content_path.write_text("No synthesis marker. " * 10, encoding="utf-8")

        rule = _rule(CHECK_RULES, "D3")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1
        assert any("synthesis" in f.lower() for f in failures)

    def test_valid_content_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)

        rule = _rule(CHECK_RULES, "D3")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert failures == []

    # F2: the column is parsed by the shared parse_synthesis_statements
    # helper (identical semantics to the validation DAG's _decode_statements)
    # — a JSON array drives the check, garbage/non-list columns degrade to
    # the file-marker path instead of crashing.

    def test_column_array_matching_file_markers_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute(
            "UPDATE node SET synthesis_statements = ? WHERE id = ?",
            (
                json.dumps(
                    ["The author implies a broader pattern beyond what is stated directly."]
                ),
                deriv_id,
            ),
        )
        con.commit()

        rule = _rule(CHECK_RULES, "D3")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert failures == []

    def test_garbage_column_falls_back_to_file_markers(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute(
            "UPDATE node SET synthesis_statements = ? WHERE id = ?",
            ("not json", deriv_id),
        )
        con.commit()

        rule = _rule(CHECK_RULES, "D3")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert failures == []

    def test_non_list_column_falls_back_to_file_markers(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute(
            "UPDATE node SET synthesis_statements = ? WHERE id = ?",
            ('{"a": 1}', deriv_id),
        )
        con.commit()

        rule = _rule(CHECK_RULES, "D3")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert failures == []


class TestCheckRuleD4:
    """D4: Size bounds."""

    def test_empty_content_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        content_path.write_text("", encoding="utf-8")

        rule = _rule(CHECK_RULES, "D4")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1

    def test_too_short_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        content_path.write_text("> Synthesis: short", encoding="utf-8")

        rule = _rule(CHECK_RULES, "D4")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1

    def test_too_long_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        content_path.write_text("> Synthesis: too long\n" + "x" * (MAX_CHARS + 1), encoding="utf-8")

        rule = _rule(CHECK_RULES, "D4")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1

    def test_within_bounds_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)

        rule = _rule(CHECK_RULES, "D4")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert failures == []


class TestCheckRuleD5:
    """D5: Tier/depth consistency."""

    def test_notes_wrong_depth(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET depth = 0 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()

        assert len(failures) >= 1
        assert any("Tier/depth" in f for f in failures)

    def test_notes_depth_1_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert failures == []

    def test_notes_depth_2_from_extracted_parent_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET depth = 1 WHERE kind = 'raw_source'")
        con.execute("UPDATE node SET depth = 2 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert failures == []

    def test_notes_depth_1_from_extracted_parent_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET depth = 1 WHERE kind = 'raw_source'")
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert len(failures) >= 1
        assert any("Tier/depth" in f for f in failures)

    def test_notes_depth_2_from_raw_source_parent_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET depth = 2 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert len(failures) >= 1
        assert any("Tier/depth" in f for f in failures)

    def test_notes_multi_parent_uses_max_depth(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET depth = 1 WHERE kind = 'raw_source'")
        parent2_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO node (id, kind, tier, trust_state, depth, created_at) "
            "VALUES (?, 'summary', 'notes', 'draft', 2, ?)",
            (parent2_id, _utcnow()),
        )
        con.execute(
            "INSERT INTO edge (id, type, relation, from_node, to_node) "
            "VALUES (?, 'provenance', 'derived_from', ?, ?)",
            (str(uuid.uuid4()), deriv_id, parent2_id),
        )
        con.execute("UPDATE node SET depth = 3 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        assert failures == []

        # Sibling notes node under the same two parents at depth 2 fails
        l0_id = con.execute("SELECT id FROM node WHERE kind = 'raw_source'").fetchone()[0]
        sibling_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at) "
            "VALUES (?, 'summary', 'notes', 'draft', 2, ?, ?)",
            (sibling_id, str(content_path), _utcnow()),
        )
        con.execute(
            "INSERT INTO edge (id, type, relation, from_node, to_node) "
            "VALUES (?, 'provenance', 'derived_from', ?, ?)",
            (str(uuid.uuid4()), sibling_id, l0_id),
        )
        con.execute(
            "INSERT INTO edge (id, type, relation, from_node, to_node) "
            "VALUES (?, 'provenance', 'derived_from', ?, ?)",
            (str(uuid.uuid4()), sibling_id, parent2_id),
        )
        con.commit()
        failures = rule.condition(con, sibling_id, content_path, content)
        con.close()
        assert len(failures) >= 1
        assert any("Tier/depth" in f for f in failures)

    def test_notes_dangling_parent_is_skipped(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        # Bypass FK (must run outside any transaction) to add a provenance edge
        # to a missing parent node: D5 must skip it, expected stays 2.
        con.execute("PRAGMA foreign_keys = OFF")
        ghost_id = str(uuid.uuid4())
        con.execute(
            "INSERT INTO edge (id, type, relation, from_node, to_node) "
            "VALUES (?, 'provenance', 'derived_from', ?, ?)",
            (str(uuid.uuid4()), deriv_id, ghost_id),
        )
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("UPDATE node SET depth = 1 WHERE kind = 'raw_source'")
        con.execute("UPDATE node SET depth = 2 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert failures == []

    def test_notes_without_parent_depth_0_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("DELETE FROM edge WHERE from_node = ?", (deriv_id,))
        con.execute("UPDATE node SET depth = 0 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert failures == []

    def test_notes_without_parent_depth_1_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("DELETE FROM edge WHERE from_node = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert len(failures) >= 1
        assert any("Tier/depth" in f for f in failures)

    def test_synthesis_depth_2_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET tier = 'synthesis', depth = 2 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert failures == []

    def test_synthesis_depth_1_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET tier = 'synthesis' WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert len(failures) >= 1

    def test_null_tier_depth_0_passes(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        l0_id = con.execute("SELECT id FROM node WHERE kind = 'raw_source'").fetchone()[0]

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, l0_id, content_path, content)
        con.close()
        assert failures == []

    def test_null_tier_depth_1_fails(self, tmp_path):
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("UPDATE node SET tier = NULL, depth = 1 WHERE id = ?", (deriv_id,))
        con.commit()

        rule = _rule(CHECK_RULES, "D5")
        content = content_path.read_text(encoding="utf-8")
        failures = rule.condition(con, deriv_id, content_path, content)
        con.close()
        assert len(failures) >= 1


# ---------------------------------------------------------------------------
# Integration: compute_node_confidence via rules
# ---------------------------------------------------------------------------

class TestComputeNodeConfidenceViaRules:
    def test_l0_node_is_low(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        assert store.compute_node_confidence(l0_id) == "low"

    def test_derived_node_is_medium(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        assert store.compute_node_confidence(deriv_id) == "medium"

    def test_two_parents_is_high(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        l0_2 = _add_l0(store)
        store.create_edge(edge_id=str(uuid.uuid4()), type="provenance",
                          relation="derived_from", from_node=deriv_id, to_node=l0_2)
        assert store.compute_node_confidence(deriv_id) == "high"

    def test_contradict_overrides_to_low(self):
        store, _ = _store()
        l0_id = _add_l0(store)
        deriv_id = _add_derived(store, l0_id)
        store.create_edge(edge_id=str(uuid.uuid4()), type="association",
                          relation="contradicts", from_node=l0_id, to_node=deriv_id)
        assert store.compute_node_confidence(deriv_id) == "low"

    def test_nonexistent_node_raises(self):
        store, _ = _store()
        with pytest.raises(ValueError, match="not found"):
            store.compute_node_confidence("nonexistent")


# ---------------------------------------------------------------------------
# Integration: run_checks via rules
# ---------------------------------------------------------------------------

class TestRunChecksViaRules:
    def test_all_pass(self, tmp_path):
        from memex.checks import run_checks
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert isinstance(result, CheckResult)
        assert result.passed is True
        assert result.failures == []

    def test_multiple_failures_all_reported(self, tmp_path):
        from memex.checks import run_checks
        con, deriv_id, content_path = _setup_check_db(tmp_path)
        con.execute("DELETE FROM edge WHERE from_node = ?", (deriv_id,))
        con.commit()
        content_path.write_text("No synthesis here.", encoding="utf-8")

        result = run_checks(con, deriv_id, content_path)
        con.close()

        assert result.passed is False
        assert len(result.failures) >= 2


# ---------------------------------------------------------------------------
# Link surface stripping (fenced + indented code) and JSON verdict parsing
# ---------------------------------------------------------------------------

class TestStripFencedBlocks:
    def test_indented_code_runs_are_dropped(self):
        """F3: 4-space-indented code blocks are not link surface — a
        [[ghost|...]] inside an indented code example is dropped, while
        links in real prose around it survive."""
        from memex.rules import _strip_fenced_blocks

        text = (
            "Prose with a real [[link|L]].\n"
            '    link = "[[ghost|G]]"\n'
            "    more code with [[ghost2|G2]]\n"
            "\n"
            "Back to prose [[real2|R]].\n"
        )
        stripped = _strip_fenced_blocks(text)
        assert "[[ghost|G]]" not in stripped
        assert "[[ghost2|G2]]" not in stripped
        assert "[[link|L]]" in stripped
        assert "[[real2|R]]" in stripped

    def test_indented_run_with_blank_line_stays_dropped(self):
        """A blank line inside an indented code run keeps the run open
        (Markdown indented code blocks may contain blank lines)."""
        from memex.rules import _strip_fenced_blocks

        text = (
            '    link = "[[ghost|G]]"\n'
            "\n"
            "    more code\n"
            "Prose resumes here [[real|R]].\n"
        )
        stripped = _strip_fenced_blocks(text)
        assert "[[ghost|G]]" not in stripped
        assert "more code" not in stripped
        assert "Prose resumes here [[real|R]]." in stripped

    def test_fenced_blocks_still_dropped(self):
        """F3 sanity: the existing fenced-region stripping is unchanged."""
        from memex.rules import _strip_fenced_blocks

        text = (
            "Prose [[real|R]].\n"
            "```python\n"
            'link = "[[ghost|G]]"\n'
            "```\n"
            "Tail prose.\n"
        )
        stripped = _strip_fenced_blocks(text)
        assert "[[ghost|G]]" not in stripped
        assert "[[real|R]]" in stripped
        assert "Tail prose." in stripped

    def test_list_line_inside_open_indented_run_is_code(self):
        """F4: a list-formatted line inside an OPEN indented code run is
        code, not a nested list item — its [[ghost]] must not leak into the
        link surface (previously the nested-list exemption dropped it into
        prose, spuriously drafting legitimate content), and the run stays
        open for the lines after it."""
        from memex.rules import _strip_fenced_blocks

        text = (
            '    code = "..."\n'
            "    - option: [[ghost|G1]]\n"
            "    more = 2\n"
            "Prose resumes [[real|R]].\n"
        )
        stripped = _strip_fenced_blocks(text)
        assert "[[ghost|G1]]" not in stripped
        assert "more = 2" not in stripped
        assert "[[real|R]]" in stripped

    def test_nested_list_item_still_link_surface(self):
        """F4/F6: a TRUE nested list item ('- Level one' then
        '    - Nested') is list continuation, not indented code — its
        [[ghost]] stays link surface (pass-5 F6 behavior preserved)."""
        from memex.rules import _strip_fenced_blocks

        text = (
            "- Level one\n"
            "    - Nested cites [[ghost|G]]\n"
            "Tail prose [[real|R]].\n"
        )
        stripped = _strip_fenced_blocks(text)
        assert "[[ghost|G]]" in stripped
        assert "[[real|R]]" in stripped

    def test_tab_indented_code_is_dropped(self):
        """F5: tab-indented lines are Markdown indented code (expanded-tab
        semantics) — a [[ghost]] on a tab-indented line is code, not link
        surface, and stays dropped inside an open run."""
        from memex.rules import _strip_fenced_blocks

        text = (
            '\tlink = "[[ghost|G]]"\n'
            "\tmore code [[ghost2|G2]]\n"
            "\n"
            "\tstill code [[ghost3|G3]]\n"
            "Prose resumes [[real|R]].\n"
        )
        stripped = _strip_fenced_blocks(text)
        assert "[[ghost|G]]" not in stripped
        assert "[[ghost2|G2]]" not in stripped
        assert "[[ghost3|G3]]" not in stripped
        assert "[[real|R]]" in stripped


class TestParseJsonVerdictReusesFenceRegex:
    def test_uses_shared_fence_regex(self):
        """F4: _parse_json_verdict reuses parsing._FENCE_RE instead of
        re-declaring the fence pattern — one fence grammar for the whole
        codebase."""
        import memex.rules as rules_mod
        from memex.utils.parsing import _FENCE_RE

        assert rules_mod._parse_json_verdict.__globals__["_FENCE_RE"] is _FENCE_RE

    def test_parses_fenced_json(self):
        from memex.rules import _parse_json_verdict

        assert _parse_json_verdict("```json\n{\"a\": 1}\n```") == {"a": 1}
        assert _parse_json_verdict("```\n{\"a\": 1}\n```") == {"a": 1}
        assert _parse_json_verdict('{"a": 1}') == {"a": 1}
        assert _parse_json_verdict("not json") is None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestMinMaxChars:
    def test_min_chars_defined(self):
        assert MIN_CHARS == 100

    def test_max_chars_defined(self):
        assert MAX_CHARS == 50_000

    def test_importable_from_checks(self):
        from memex.checks import MIN_CHARS as mc, MAX_CHARS as xc
        assert mc == 100
        assert xc == 50_000
