"""Tests for `memex list` command.

list is strictly read-only — it must not write to db or filesystem.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


FAKE_FETCHER = "tests.fake_fetcher:FakeFetcher"
WORKTREE = Path("/home/sbottiglieri/memex-issue-3")


def run_memex(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "memex.cli"] + args,
        capture_output=True,
        text=True,
        cwd=WORKTREE,
        env=full_env,
    )


@pytest.fixture()
def store(tmp_path):
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])
    return {"db": db_path, "vault": vault_path}


def ingest(store, url: str):
    return run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )


class TestList:
    def test_list_returns_empty_array_when_no_nodes(self, store):
        result = run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == []

    def test_list_returns_array_with_one_node_after_ingest(self, store):
        ingest(store, "https://example.com/article")
        result = run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 1

    def test_list_node_has_required_fields(self, store):
        ingest(store, "https://example.com/article")
        result = run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
        node = json.loads(result.stdout)[0]
        assert "id" in node
        assert node["kind"] == "raw_source"
        assert node["tier"] is None
        assert node["trust_state"] == "draft"
        assert node["canonical_key"] == "https://example.com/article"

    def test_list_returns_multiple_nodes(self, store):
        ingest(store, "https://example.com/article-1")
        ingest(store, "https://example.com/article-2")
        result = run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
        data = json.loads(result.stdout)
        assert len(data) == 2

    def test_list_does_not_write_to_db(self, store):
        """list is read-only: db mtime should not change after list."""
        import os
        import time
        ingest(store, "https://example.com/article")
        mtime_before = os.path.getmtime(store["db"])
        time.sleep(0.05)
        run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
        mtime_after = os.path.getmtime(store["db"])
        assert mtime_before == mtime_after
