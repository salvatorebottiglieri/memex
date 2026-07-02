"""Shared fixtures and helpers for memex tests."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from memex.fetcher import FetchResult, FetchError
from memex.store import Store


class FakeFetcher:
    """Deterministic fetcher for tests."""
    def fetch(self, url: str):
        if "fail.example.com" in url:
            raise FetchError(f"Simulated fetch failure for {url}")
        return FetchResult(
            content=f"# Fake Article\n\nFake content for {url}",
            title="Fake Article Title",
        )


FAKE_FETCHER = "tests.conftest:FakeFetcher"
WORKTREE = Path(__file__).resolve().parent.parent


def _run_memex(args: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    # `uv run python -m memex.cli` keeps cwd on sys.path, which makes the
    # `MEMEX_FETCHER_MODULE=tests.conftest:FakeFetcher` test seam work
    # without needing PYTHONPATH. Falls back to direct python if uv is absent.
    if shutil.which("uv"):
        cmd = ["uv", "run", "python", "-m", "memex.cli", *args]
    else:
        cmd = [sys.executable, "-m", "memex.cli", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=full_env,
    )


@pytest.fixture
def run_memex():
    """Fixture that provides the run_memex helper."""
    return _run_memex


@pytest.fixture()
def db_store(tmp_path):
    """In-memory Store with schema initialised (for direct unit tests)."""
    con = sqlite3.connect(":memory:")
    store = Store(con)
    store.init_schema()
    return store


@pytest.fixture()
def store(tmp_path):
    """Initialised db + vault (for CLI subprocess tests)."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    _run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)], cwd=tmp_path)
    return {"db": db_path, "vault": vault_path, "tmp": tmp_path}


def ingest(store, url: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run memex ingest with the fake fetcher."""
    env = {"MEMEX_FETCHER_MODULE": FAKE_FETCHER, **(extra_env or {})}
    return _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        cwd=WORKTREE,
        env=env,
    )
