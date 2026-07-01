"""Tests for `memex show <id>` command.

show is strictly read-only — it must not write to db or filesystem.
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


def ingest(store, url: str) -> dict:
    result = run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    return json.loads(result.stdout)


def show(store, node_id: str):
    return run_memex(
        ["show", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
    )


class TestShow:
    def test_show_returns_json_for_known_id(self, store):
        ingested = ingest(store, "https://example.com/article")
        result = show(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["id"] == ingested["id"]

    def test_show_includes_content(self, store):
        ingested = ingest(store, "https://example.com/article")
        result = show(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["content"] is not None
        assert "Fake content" in data["content"]

    def test_show_includes_canonical_key(self, store):
        ingested = ingest(store, "https://example.com/article?utm_source=test")
        result = show(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["canonical_key"] == "https://example.com/article"

    def test_show_includes_source_url(self, store):
        url = "https://example.com/article"
        ingested = ingest(store, url)
        result = show(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["source_url"] == url

    def test_show_includes_l0_path(self, store):
        ingested = ingest(store, "https://example.com/article")
        result = show(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["l0_path"] is not None
        assert Path(data["l0_path"]).exists()

    def test_show_includes_trust_state(self, store):
        ingested = ingest(store, "https://example.com/article")
        result = show(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["trust_state"] == "draft"

    def test_show_returns_error_for_unknown_id(self, store):
        result = show(store, "00000000-0000-0000-0000-000000000000")
        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["error"] == "not_found"

    def test_show_does_not_write_to_db(self, store):
        import os
        import time
        ingested = ingest(store, "https://example.com/article")
        mtime_before = os.path.getmtime(store["db"])
        time.sleep(0.05)
        show(store, ingested["id"])
        mtime_after = os.path.getmtime(store["db"])
        assert mtime_before == mtime_after
