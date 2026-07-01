"""Tests for `memex ingest <url>`.

ContentFetcher is injected via MEMEX_FETCHER_MODULE env var — no real network.
The fake fetcher module lives at tests/fake_fetcher.py.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


FAKE_FETCHER = "tests.fake_fetcher:FakeFetcher"


def run_memex(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
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
        cwd=tmp_path,
    )
    return {"db": db_path, "vault": vault_path, "tmp": tmp_path}


def ingest(store, url: str, extra_env: dict | None = None):
    env = {"MEMEX_FETCHER_MODULE": FAKE_FETCHER, **(extra_env or {})}
    return run_memex(
        [
            "ingest",
            "--db", str(store["db"]),
            "--vault", str(store["vault"]),
            url,
        ],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
    )


class TestIngestHappyPath:
    def test_ingest_returns_json_with_node_id(self, store):
        result = ingest(store, "https://example.com/article")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert "id" in data
        assert data["status"] == "ingested"

    def test_ingest_writes_l0_markdown_file(self, store):
        ingest(store, "https://example.com/article")
        vault = store["vault"]
        md_files = list(vault.glob("*.md"))
        assert len(md_files) == 1

    def test_l0_file_contains_fetched_content(self, store):
        ingest(store, "https://example.com/article")
        vault = store["vault"]
        md_file = next(vault.glob("*.md"))
        content = md_file.read_text()
        # The fake fetcher returns "Fake content for <url>"
        assert "Fake content" in content

    def test_ingest_inserts_node_row(self, store):
        ingest(store, "https://example.com/article")
        con = sqlite3.connect(store["db"])
        rows = con.execute("SELECT id, kind, trust_state, depth FROM node").fetchall()
        con.close()
        assert len(rows) == 1
        _id, kind, trust_state, depth = rows[0]
        assert kind == "raw_source"
        assert trust_state == "draft"
        assert depth == 0

    def test_ingest_inserts_source_row_with_canonical_key(self, store):
        url = "https://example.com/article?utm_source=twitter"
        ingest(store, url)
        con = sqlite3.connect(store["db"])
        rows = con.execute(
            "SELECT canonical_key, source_url, title FROM source"
        ).fetchall()
        con.close()
        assert len(rows) == 1
        canonical, source_url, title = rows[0]
        assert canonical == "https://example.com/article"  # tracking param stripped
        assert source_url == url
        assert title is not None

    def test_ingest_source_row_records_fetched_at(self, store):
        ingest(store, "https://example.com/article")
        con = sqlite3.connect(store["db"])
        row = con.execute("SELECT fetched_at FROM source").fetchone()
        con.close()
        assert row is not None
        assert row[0] is not None  # fetched_at is set


class TestIngestIdempotency:
    def test_ingesting_same_url_twice_yields_one_node(self, store):
        ingest(store, "https://example.com/article")
        result2 = ingest(store, "https://example.com/article")
        assert result2.returncode == 0, result2.stderr

        con = sqlite3.connect(store["db"])
        count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        con.close()
        assert count == 1

    def test_ingesting_same_url_twice_second_returns_already_exists(self, store):
        ingest(store, "https://example.com/article")
        result2 = ingest(store, "https://example.com/article")
        data = json.loads(result2.stdout)
        assert data["status"] == "already_exists"

    def test_tracking_param_variant_deduped_to_same_node(self, store):
        """URLs that differ only in tracking params collapse to the same canonical key."""
        ingest(store, "https://example.com/article")
        result2 = ingest(store, "https://example.com/article?utm_campaign=test")
        data = json.loads(result2.stdout)
        assert data["status"] == "already_exists"

    def test_already_exists_response_includes_failed_false_for_successful_prior_ingest(self, store):
        """already_exists response includes failed=false when the prior ingest succeeded."""
        ingest(store, "https://example.com/article")
        result2 = ingest(store, "https://example.com/article")
        data = json.loads(result2.stdout)
        assert data["status"] == "already_exists"
        assert data["failed"] is False

    def test_already_exists_response_includes_failed_true_for_prior_failed_ingest(self, store):
        """already_exists response includes failed=true when the prior ingest failed, so the
        agent knows it should retry rather than assume the node has content."""
        ingest(store, "https://fail.example.com/article")  # first attempt fails
        result2 = ingest(store, "https://fail.example.com/article")  # second attempt
        data = json.loads(result2.stdout)
        assert data["status"] == "already_exists"
        assert data["failed"] is True


class TestIngestFetchFailure:
    def test_fetch_failure_does_not_crash(self, store):
        """A URL that the fake fetcher cannot fetch records a failure and exits 0."""
        # Tell the fake fetcher to fail this URL by passing a special marker
        result = ingest(store, "https://fail.example.com/article")
        assert result.returncode == 0, result.stderr

    def test_fetch_failure_returns_json_with_failed_status(self, store):
        result = ingest(store, "https://fail.example.com/article")
        data = json.loads(result.stdout)
        assert data["status"] == "fetch_failed"

    def test_fetch_failure_records_source_row_with_failed_flag(self, store):
        ingest(store, "https://fail.example.com/article")
        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT failed FROM source WHERE source_url = 'https://fail.example.com/article'"
        ).fetchone()
        con.close()
        assert row is not None
        assert row[0] == 1  # failed = true
