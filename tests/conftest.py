"""Shared fixtures and helpers for memex tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


class FakeFetcher:
    """Deterministic fetcher for tests."""
    def fetch(self, url: str):
        from memex.fetcher import FetchResult, FetchError
        if "fail.example.com" in url:
            raise FetchError(f"Simulated fetch failure for {url}")
        return FetchResult(
            content=f"# Fake Article\n\nFake content for {url}",
            title="Fake Article Title",
        )


FAKE_FETCHER = "tests.conftest:FakeFetcher"
WORKTREE = Path(__file__).resolve().parent.parent


SRC_DIR = str(WORKTREE / "src")


def _run_memex(args: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    full_env = {**os.environ, **{"PYTHONPATH": SRC_DIR}, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "memex.cli"] + args,
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
def store(tmp_path):
    """Initialised db + vault."""
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
