"""Tests for `memex relate <source_id> <target_id> [--relation]`."""
from __future__ import annotations

import json
import sqlite3

from tests.conftest import WORKTREE, _run_memex, register_node


class TestRelate:
    def _create_node(self, store, label: str) -> str:
        """Register a test node and return its id."""
        result = register_node(
            store, store["vault"], f"{label}.md",
            f"https://example.com/{label}",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["id"]

    def test_relate_returns_json_with_edge_id(self, store):
        a_id = self._create_node(store, "article-a")
        b_id = self._create_node(store, "article-b")
        result = _run_memex(
            ["relate", "--db", str(store["db"]), "--vault", str(store["vault"]),
             a_id, b_id, "--relation", "related"],
            cwd=WORKTREE,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert "edge_id" in data
        assert data["source_id"] == a_id
        assert data["target_id"] == b_id
        assert data["relation"] == "related"

    def test_relate_persists_edge_in_db(self, store):
        a_id = self._create_node(store, "article-a")
        b_id = self._create_node(store, "article-b")
        result = _run_memex(
            ["relate", "--db", str(store["db"]), "--vault", str(store["vault"]),
             a_id, b_id],
            cwd=WORKTREE,
        )
        data = json.loads(result.stdout)
        conn = sqlite3.connect(store["db"])
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT type, relation, from_node, to_node, written_by FROM edge WHERE id = ?",
            (data["edge_id"],),
        ).fetchone()
        conn.close()
        assert row["type"] == "association"
        assert row["relation"] == "related"

    def test_relate_refines_uses_refines_relation(self, store):
        a_id = self._create_node(store, "a")
        b_id = self._create_node(store, "b")
        result = _run_memex(
            ["relate", "--db", str(store["db"]), "--vault", str(store["vault"]),
             a_id, b_id, "--relation", "refines"],
            cwd=WORKTREE,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["relation"] == "refines"

    def test_relate_self_fails(self, store):
        a_id = self._create_node(store, "a")
        result = _run_memex(
            ["relate", "--db", str(store["db"]), "--vault", str(store["vault"]),
             a_id, a_id],
            cwd=WORKTREE,
        )
        assert result.returncode == 1
        data = json.loads(result.stderr)
        assert "cannot_relate_to_self" in data.get("error", "")

    def test_relate_does_not_create_event(self, store):
        a_id = self._create_node(store, "a")
        b_id = self._create_node(store, "b")
        _run_memex(
            ["relate", "--db", str(store["db"]), "--vault", str(store["vault"]),
             a_id, b_id],
            cwd=WORKTREE,
        )
        conn = sqlite3.connect(store["db"])
        count = conn.execute("SELECT COUNT(*) FROM event_queue").fetchone()[0]
        conn.close()
        assert count == 0

    def test_relate_does_not_affect_confidence(self, store):
        a_id = self._create_node(store, "a")
        b_id = self._create_node(store, "b")
        conn = sqlite3.connect(store["db"])
        conn.row_factory = sqlite3.Row
        conf_before = conn.execute(
            "SELECT confidence FROM node WHERE id = ?", (a_id,)
        ).fetchone()["confidence"]
        conn.close()
        _run_memex(
            ["relate", "--db", str(store["db"]), "--vault", str(store["vault"]),
             a_id, b_id],
            cwd=WORKTREE,
        )
        conn = sqlite3.connect(store["db"])
        conn.row_factory = sqlite3.Row
        conf_after = conn.execute(
            "SELECT confidence FROM node WHERE id = ?", (a_id,)
        ).fetchone()["confidence"]
        conn.close()
        assert conf_after == conf_before
