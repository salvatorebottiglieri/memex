"""Tests for `memex derive <node-id>` and `memex search <query>`.

LLMClient is injected via MEMEX_LLM_MODULE — no real Anthropic calls.
The fake LLM client module lives at tests/fake_llm_client.py.
"""
from __future__ import annotations

import json

from tests.conftest import _run_memex, FAKE_FETCHER


FAKE_LLM = "tests.fake_llm_client:FakeLLMClient"


def _ingest(store, url: str) -> dict:
    result = _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _derive(store, node_id: str) -> "subprocess.CompletedProcess":  # type: ignore[name-defined]
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_LLM_MODULE": FAKE_LLM},
    )


class TestDerive:
    def test_derive_returns_json_with_derivation_id(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert "id" in data
        assert data["status"] == "derived"

    def test_derive_inserts_notes_tier_node(self, store):
        """The derivation node has kind=summary, tier=notes, depth=1.

        FakeLLMClient produces a valid derivation (has synthesis marker, right length),
        so trust_state is auto-verified after checks run.
        """
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        deriv_id = json.loads(result.stdout)["id"]

        import sqlite3
        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT kind, tier, trust_state, depth FROM node WHERE id = ?", (deriv_id,)
        ).fetchone()
        con.close()

        assert row is not None
        kind, tier, trust_state, depth = row
        assert kind == "summary"
        assert tier == "notes"
        assert trust_state == "auto-verified"
        assert depth == 1

    def test_derive_inserts_provenance_edge(self, store):
        ingested = _ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        result = _derive(store, l0_id)
        deriv_id = json.loads(result.stdout)["id"]

        import sqlite3
        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT type, relation, from_node, to_node FROM edge "
            "WHERE from_node = ? AND to_node = ?",
            (deriv_id, l0_id),
        ).fetchone()
        con.close()

        assert row is not None
        assert row[0] == "provenance"
        assert row[1] == "derived_from"
        assert row[2] == deriv_id
        assert row[3] == l0_id

    def test_derive_writes_markdown_file_with_synthesis_markers(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        deriv_id = json.loads(result.stdout)["id"]

        md_path = store["vault"] / f"{deriv_id}.md"
        assert md_path.exists()
        assert "> Synthesis:" in md_path.read_text(encoding="utf-8")

    def test_derive_response_includes_l0_node_id(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["l0_node_id"] == ingested["id"]

    def test_derive_is_idempotent(self, store):
        """Deriving the same L0 twice produces one summary node and one edge."""
        ingested = _ingest(store, "https://example.com/article")
        l0_id = ingested["id"]

        first = _derive(store, l0_id)
        assert first.returncode == 0, first.stderr
        first_data = json.loads(first.stdout)
        assert first_data["status"] == "derived"

        second = _derive(store, l0_id)
        assert second.returncode == 0, second.stderr
        second_data = json.loads(second.stdout)
        assert second_data["status"] == "already_derived"

        import sqlite3
        con = sqlite3.connect(store["db"])
        node_count = con.execute(
            "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
        ).fetchone()[0]
        edge_count = con.execute(
            "SELECT COUNT(*) FROM edge WHERE to_node = ? "
            "AND type = 'provenance' AND relation = 'derived_from'",
            (l0_id,),
        ).fetchone()[0]
        con.close()
        assert node_count == 1
        assert edge_count == 1

    def test_derive_unknown_node_returns_error(self, store):
        result = _derive(store, "does-not-exist")
        assert result.returncode != 0
        data = json.loads(result.stderr)
        assert data["error"] == "not_found"


class TestSearch:
    def _search(self, store, query: str):
        return _run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), query],
        )

    def test_search_returns_json_array(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])
        result = self._search(store, "Synthesis")
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_search_matches_derivation_content(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert len(data) >= 1

    def test_search_result_has_required_fields(self, store):
        ingested = _ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        _derive(store, l0_id)
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert len(data) >= 1
        item = data[0]
        assert "id" in item
        assert "snippet" in item
        assert "canonical_key" in item
        assert "l0_node_id" in item

    def test_search_snippet_contains_query(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert "broader pattern" in data[0]["snippet"].lower()

    def test_search_returns_empty_array_for_no_match(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])
        result = self._search(store, "xyznonexistentterm")
        assert json.loads(result.stdout) == []

    def test_search_is_readonly(self, store):
        import sqlite3
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])

        con = sqlite3.connect(store["db"])
        n_before = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        e_before = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()

        self._search(store, "broader pattern")

        con = sqlite3.connect(store["db"])
        n_after = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        e_after = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()
        assert n_before == n_after
        assert e_before == e_after

    def test_search_l0_node_id_points_to_l0(self, store):
        ingested = _ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        _derive(store, l0_id)
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert data[0]["l0_node_id"] == l0_id