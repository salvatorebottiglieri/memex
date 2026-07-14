"""Tests for `memex synthesize <id1> <id2> ...`.

Agent is injected via MEMEX_AGENT — no real Anthropic calls.
The fake agent module lives at tests/fake_llm_client.py.
"""
from __future__ import annotations

import json
import sqlite3

from tests.conftest import _run_memex, FAKE_FETCHER


FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_THROWS_AGENT = "tests.fake_llm_client_throws:FakeLLMClientThrows"


def _ingest(store, url: str) -> dict:
    result = _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _synthesize(store, *node_ids: str) -> "subprocess.CompletedProcess":  # type: ignore[name-defined]
    return _run_memex(
        ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), *node_ids],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )


class TestSynthesize:
    def test_synthesize_returns_json_with_synthesis_id(self, store):
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "synthesized"
        assert isinstance(data["id"], str)
        assert len(data["id"]) > 0

    def test_synthesize_node_fields(self, store):
        """The synthesis node has kind=summary, tier=synthesis, depth=parent.depth+1."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        conn = sqlite3.connect(store["db"])
        try:
            row = conn.execute(
                "SELECT kind, tier, depth FROM node WHERE id = ?", (data["id"],)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "Synthesis node not found in store"
        kind, tier, depth = row
        assert kind == "summary"
        assert tier == "synthesis"
        # Both L0 nodes have depth=0, so max + 1 = 1
        assert depth == 1

    def test_synthesize_provenance_edges(self, store):
        """Synthesis creates N derived_from edges, one per parent."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        c = _ingest(store, "https://example.com/article-c")
        result = _synthesize(store, a["id"], b["id"], c["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        conn = sqlite3.connect(store["db"])
        try:
            edges = conn.execute(
                """
                SELECT to_node FROM edge
                WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
                ORDER BY to_node
                """,
                (data["id"],),
            ).fetchall()
        finally:
            conn.close()

        parent_ids = sorted([a["id"], b["id"], c["id"]])
        edge_targets = sorted(r[0] for r in edges)
        assert edge_targets == parent_ids

    def test_synthesize_writes_markdown_file(self, store):
        """Synthesis writes a .md file with derivation prose."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        md_path = store["vault"] / f"{data['id']}.md"
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "> Synthesis:" in content

    def test_synthesize_includes_parent_ids(self, store):
        """Response includes the list of parent node ids."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["parent_ids"] == [a["id"], b["id"]]

    def test_synthesize_is_idempotent(self, store):
        """Same set of parent ids produces already_synthesized on second call."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        r1 = _synthesize(store, a["id"], b["id"])
        assert r1.returncode == 0, r1.stderr
        d1 = json.loads(r1.stdout)
        assert d1["status"] == "synthesized"

        # Reverse order — should still match same unordered set
        r2 = _synthesize(store, b["id"], a["id"])
        assert r2.returncode == 0, r2.stderr
        d2 = json.loads(r2.stdout)
        assert d2["status"] == "already_synthesized"
        assert d2["id"] == d1["id"]

        # Verify only one node and N edges exist
        conn = sqlite3.connect(store["db"])
        try:
            node_count = conn.execute(
                "SELECT COUNT(*) FROM node WHERE id = ?", (d1["id"],)
            ).fetchone()[0]
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM edge WHERE from_node = ?", (d1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert node_count == 1
        assert edge_count == 2

    def test_synthesize_unknown_parent_returns_error(self, store):
        """Unknown parent node returns not_found error."""
        result = _synthesize(store, "does-not-exist")
        assert result.returncode == 1
        data = json.loads(result.stderr)
        assert data["error"] == "not_found"
        assert data["node_id"] == "does-not-exist"

    def test_synthesize_single_parent(self, store):
        """Synthesize with a single parent is a valid edge case."""
        a = _ingest(store, "https://example.com/article-a")
        result = _synthesize(store, a["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "synthesized"
        assert len(data["parent_ids"]) == 1
        assert data["parent_ids"][0] == a["id"]

        # Should still be tier=synthesis, depth=1
        conn = sqlite3.connect(store["db"])
        try:
            row = conn.execute(
                "SELECT kind, tier, depth FROM node WHERE id = ?", (data["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "summary"
        assert row[1] == "synthesis"
        assert row[2] == 1

    def test_synthesize_agent_failure_returns_error(self, store):
        """When the agent raises, the CLI returns agent_failed."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _run_memex(
            ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), a["id"], b["id"]],
            env={"MEMEX_AGENT": FAKE_THROWS_AGENT},
        )
        data = json.loads(result.stderr)
        assert data["error"] == "agent_failed"
        assert "detail" in data

    def test_synthesize_multiple_parents_depth_calculation(self, store):
        """Depth is max(parent.depth) + 1, not just 1."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")

        # Synthesize a and b to create a node with depth=1
        r1 = _synthesize(store, a["id"], b["id"])
        assert r1.returncode == 0, r1.stderr
        d1 = json.loads(r1.stdout)

        # Now synthesize the first synthesis + one L0 → depth=max(1,0)+1 = 2
        r2 = _synthesize(store, d1["id"], a["id"])
        assert r2.returncode == 0, r2.stderr
        d2 = json.loads(r2.stdout)
        assert d2["status"] == "synthesized"

        conn = sqlite3.connect(store["db"])
        try:
            row = conn.execute(
                "SELECT depth FROM node WHERE id = ?", (d2["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 2, f"Expected depth 2, got {row[0]}"
