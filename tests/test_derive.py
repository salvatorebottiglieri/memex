"""Tests for `memex derive <node-id>` and `memex search <query>`.

LLMClient is injected via MEMEX_LLM_MODULE env var — no real Anthropic calls.
The fake LLM client module lives at tests/fake_llm_client.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


FAKE_FETCHER = "tests.fake_fetcher:FakeFetcher"
FAKE_LLM = "tests.fake_llm_client:FakeLLMClient"
WORKTREE = Path("/home/sbottiglieri/memex-issue-5")


def run_memex(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "memex.cli"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=full_env,
    )


@pytest.fixture()
def store(tmp_path):
    """Initialised db + vault."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    run_memex(
        ["init", "--db", str(db_path), "--vault", str(vault_path)],
        cwd=WORKTREE,
    )
    return {"db": db_path, "vault": vault_path, "tmp": tmp_path}


def ingest(store, url: str) -> dict:
    env = {"MEMEX_FETCHER_MODULE": FAKE_FETCHER}
    result = run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        cwd=WORKTREE,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def derive(store, node_id: str) -> subprocess.CompletedProcess:
    env = {"MEMEX_LLM_MODULE": FAKE_LLM}
    return run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        cwd=WORKTREE,
        env=env,
    )


class TestDerive:
    def test_derive_returns_json_with_derivation_id(self, store):
        """Tracer bullet: derive produces a derivation node and returns its id."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]

        result = derive(store, l0_id)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert "id" in data
        assert data["status"] == "derived"

    def test_derive_inserts_notes_tier_node(self, store):
        """The derivation node has tier=notes, kind=summary, trust_state=draft, depth=1."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        result = derive(store, l0_id)
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT kind, tier, trust_state, depth FROM node WHERE id = ?",
            (deriv_id,),
        ).fetchone()
        con.close()

        assert row is not None
        kind, tier, trust_state, depth = row
        assert kind == "summary"
        assert tier == "notes"
        assert trust_state == "draft"
        assert depth == 1

    def test_derive_inserts_provenance_edge(self, store):
        """A derived_from edge exists linking the derivation to its L0."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        result = derive(store, l0_id)
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            """
            SELECT type, relation, from_node, to_node
            FROM edge
            WHERE from_node = ? AND to_node = ?
            """,
            (deriv_id, l0_id),
        ).fetchone()
        con.close()

        assert row is not None
        edge_type, relation, from_node, to_node = row
        assert edge_type == "provenance"
        assert relation == "derived_from"
        assert from_node == deriv_id
        assert to_node == l0_id

    def test_derive_writes_markdown_file_with_synthesis_markers(self, store):
        """The derivation prose is stored as a markdown file with > Synthesis: markers."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        result = derive(store, l0_id)
        deriv_id = json.loads(result.stdout)["id"]

        # The markdown file should be <deriv_id>.md in the vault
        md_path = store["vault"] / f"{deriv_id}.md"
        assert md_path.exists(), f"Expected {md_path} to exist"
        content = md_path.read_text(encoding="utf-8")
        assert "> Synthesis:" in content

    def test_derive_node_id_is_returned_as_l0_id(self, store):
        """The response includes the l0_node_id for traceability."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        result = derive(store, l0_id)
        data = json.loads(result.stdout)
        assert data["l0_node_id"] == l0_id

    def test_derive_is_idempotent(self, store):
        """Deriving the same L0 twice produces exactly one summary node and one edge."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]

        first = derive(store, l0_id)
        assert first.returncode == 0, first.stderr
        first_data = json.loads(first.stdout)
        assert first_data["status"] == "derived"

        second = derive(store, l0_id)
        assert second.returncode == 0, second.stderr
        second_data = json.loads(second.stdout)
        assert second_data["status"] == "already_derived"

        con = sqlite3.connect(store["db"])
        node_count = con.execute(
            "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
        ).fetchone()[0]
        edge_count = con.execute(
            "SELECT COUNT(*) FROM edge WHERE to_node = ? AND type = 'provenance' AND relation = 'derived_from'",
            (l0_id,),
        ).fetchone()[0]
        con.close()

        assert node_count == 1
        assert edge_count == 1


class TestSearch:
    def test_search_returns_json_array(self, store):
        """memex search returns a JSON array."""
        ingested = ingest(store, "https://example.com/article")
        derive(store, ingested["id"])

        result = run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), "Synthesis"],
            cwd=WORKTREE,
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_search_matches_derivation_content(self, store):
        """search finds derivations containing the query term."""
        ingested = ingest(store, "https://example.com/article")
        derive(store, ingested["id"])

        result = run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), "broader pattern"],
            cwd=WORKTREE,
        )
        data = json.loads(result.stdout)
        assert len(data) >= 1

    def test_search_result_has_required_fields(self, store):
        """Each search result has id, snippet, canonical_key, and l0_node_id."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        derive(store, ingested["id"])

        result = run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), "broader pattern"],
            cwd=WORKTREE,
        )
        data = json.loads(result.stdout)
        assert len(data) >= 1
        item = data[0]
        assert "id" in item
        assert "snippet" in item
        assert "canonical_key" in item
        assert "l0_node_id" in item

    def test_search_snippet_contains_query(self, store):
        """The snippet field contains a portion of the matched derivation content."""
        ingested = ingest(store, "https://example.com/article")
        derive(store, ingested["id"])

        result = run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), "broader pattern"],
            cwd=WORKTREE,
        )
        data = json.loads(result.stdout)
        item = data[0]
        assert "broader pattern" in item["snippet"].lower()

    def test_search_returns_empty_array_for_no_match(self, store):
        """search returns [] when no derivations match."""
        ingested = ingest(store, "https://example.com/article")
        derive(store, ingested["id"])

        result = run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), "xyznonexistentterm"],
            cwd=WORKTREE,
        )
        data = json.loads(result.stdout)
        assert data == []

    def test_search_is_readonly(self, store):
        """search does not write any new rows to the database."""
        ingested = ingest(store, "https://example.com/article")
        derive(store, ingested["id"])

        con = sqlite3.connect(store["db"])
        node_count_before = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        edge_count_before = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()

        run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), "broader pattern"],
            cwd=WORKTREE,
        )

        con = sqlite3.connect(store["db"])
        node_count_after = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        edge_count_after = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()

        assert node_count_before == node_count_after
        assert edge_count_before == edge_count_after

    def test_search_l0_node_id_points_to_l0(self, store):
        """The l0_node_id in search results matches the original L0 node."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        derive(store, ingested["id"])

        result = run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), "broader pattern"],
            cwd=WORKTREE,
        )
        data = json.loads(result.stdout)
        assert data[0]["l0_node_id"] == l0_id
