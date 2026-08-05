"""Tests for extract, ideas, delete, retry, stats, and list filtering commands.

Agent/mock conventions follow test_derive.py:
- FAKE_AGENT points to the deterministic FakeLLMClient
- FAKE_THROWS_AGENT triggers agent_failed errors
- _run_memex from conftest for all CLI subprocess calls
- store fixture from conftest for db+vault lifecycle
"""
from __future__ import annotations

import json

from tests.conftest import _run_memex, register_node

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_THROWS_AGENT = "tests.fake_llm_client_throws:FakeLLMClientThrows"


def _ingest(store, url: str) -> dict:
    """Ingest a URL and return the parsed JSON result."""
    filename = url.rsplit("/", 1)[-1].split("?", 1)[0] + ".md"
    result = register_node(store, store["vault"], filename, url)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _derive(store, node_id: str):
    """Derive a node using the fake agent."""
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )


def _extract(store, node_id: str, *, agent: str = FAKE_AGENT):
    """Run memex extract-ideas on a node."""
    return _run_memex(
        ["extract-ideas", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": agent},
    )


def _ideas(store, query: str = ""):
    """Run memex ideas with optional query."""
    args = ["ideas", "--db", str(store["db"]), "--vault", str(store["vault"])]
    if query:
        args.append(query)
    return _run_memex(args)


# ── TestExtract ──────────────────────────────────────────────────────


class TestExtract:
    def test_extract_produces_ideas(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _extract(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["node_id"] == ingested["id"]
        assert isinstance(data["ideas_count"], int)
        assert data["ideas_count"] >= 1
        assert isinstance(data["ideas"], list)
        assert len(data["ideas"]) == data["ideas_count"]

    def test_extract_idempotent(self, store):
        ingested = _ingest(store, "https://example.com/article")
        # First extract
        _extract(store, ingested["id"])
        # Second extract replaces ideas (idempotent)
        result = _extract(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["node_id"] == ingested["id"]
        # FakeAgent always returns exactly 3 ideas
        assert data["ideas_count"] == 3
        assert len(data["ideas"]) == 3

    def test_extract_unknown_node(self, store):
        result = _extract(store, "nonexistent-id")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["error"] == "not_found"

    def test_extract_agent_failure(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _extract(store, ingested["id"], agent=FAKE_THROWS_AGENT)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["error"] == "agent_failed"


# ── TestIdeas ────────────────────────────────────────────────────────


class TestIdeas:
    def test_ideas_search(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _extract(store, ingested["id"])

        result = _ideas(store, "Key idea")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) >= 1
        item = data[0]
        assert "idea_text" in item
        assert "match_rank" in item
        assert "node_id" in item
        assert "node_kind" in item
        assert "node_tier" in item

    def test_ideas_no_match(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _extract(store, ingested["id"])

        result = _ideas(store, "xyznonexistentterm")
        assert json.loads(result.stdout) == []

    def test_ideas_empty_query(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _extract(store, ingested["id"])

        result = _ideas(store)
        data = json.loads(result.stdout)
        # Empty query returns all ideas (FakeAgent always produces 3)
        assert len(data) == 3


# ── TestListFiltering ────────────────────────────────────────────────


class TestListFiltering:
    def test_list_filter_by_kind(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])

        # List by kind=url -> exactly 1 result
        result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "--kind", "url"],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["kind"] == "url"

        # List by kind=summary -> exactly 1 result
        result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "--kind", "summary"],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["kind"] == "summary"

    def test_list_limit(self, store):
        _ingest(store, "https://example.com/article-1")
        _ingest(store, "https://example.com/article-2")

        result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "--limit", "1"],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 1


# ── TestDeleteCommand ───────────────────────────────────────────────


class TestDeleteCommand:
    def test_delete_basic(self, store):
        """Deleting the URL-node cascades to the extracted-node pair."""
        ingested = _ingest(store, "https://example.com/article")
        result = _run_memex(
            ["delete", "--db", str(store["db"]), "--vault", str(store["vault"]),
             ingested["url_node_id"], "--cascade"],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "deleted"
        assert ingested["url_node_id"] in data["removed"]
        assert ingested["id"] in data["removed"]  # extracted node too

        # List confirms both nodes are gone
        list_result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert json.loads(list_result.stdout) == []

    def test_delete_nonexistent(self, store):
        result = _run_memex(
            ["delete", "--db", str(store["db"]), "--vault", str(store["vault"]),
             "nonexistent-id"],
        )
        assert result.returncode != 0
        data = json.loads(result.stderr)
        assert data["error"] == "not_found"

    def test_delete_cascade(self, store):
        """Cascading from the URL-node removes url + extracted + derivation."""
        ingested = _ingest(store, "https://example.com/article")
        derive_result = _derive(store, ingested["id"])
        assert derive_result.returncode == 0, derive_result.stderr
        deriv_id = json.loads(derive_result.stdout)["id"]

        result = _run_memex(
            ["delete", "--db", str(store["db"]), "--vault", str(store["vault"]),
             ingested["url_node_id"], "--cascade"],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "deleted"
        # url + extracted + derived summary — the recursive cascade is deduped
        # by a visited set, so each id is reported exactly once
        assert len(data["removed"]) == 3
        assert set(data["removed"]) == {
            ingested["url_node_id"], ingested["id"], deriv_id,
        }

        # List confirms all are gone
        list_result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert json.loads(list_result.stdout) == []




# ── TestStatsCommand ────────────────────────────────────────────────


class TestStatsCommand:
    def test_stats_basic(self, store):
        _ingest(store, "https://example.com/article")
        result = _run_memex(
            ["stats", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["total_nodes"] >= 1
        assert "by_kind" in data
        assert "by_tier" in data
        assert "by_trust_state" in data
        assert "by_confidence" in data


# ── ExtractIdeas batch (--all) ───────────────────────────────────────


class TestExtractIdeasBatch:
    def test_extract_all_batch_and_skip(self, store):
        _ingest(store, "https://example.com/batch-a")
        _ingest(store, "https://example.com/batch-b")
        p = _run_memex(
            ["extract-ideas", "--all", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )
        assert p.returncode == 0, p.stderr
        results = json.loads(p.stdout)
        assert [r["status"] for r in results].count("extracted") == 2
        # Re-run: nodes that already have ideas are skipped, not re-called.
        p2 = _run_memex(
            ["extract-ideas", "--all", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )
        assert p2.returncode == 0, p2.stderr
        assert all(r["status"] == "already_extracted" for r in json.loads(p2.stdout))

    def test_extract_all_limit(self, store):
        _ingest(store, "https://example.com/batch-1")
        _ingest(store, "https://example.com/batch-2")
        p = _run_memex(
            ["extract-ideas", "--all", "--limit", "1",
             "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )
        assert p.returncode == 0, p.stderr
        results = json.loads(p.stdout)
        assert [r["status"] for r in results].count("extracted") == 1

    def test_extract_without_node_id_or_all_fails(self, store):
        p = _run_memex(
            ["extract-ideas", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )
        assert p.returncode != 0
