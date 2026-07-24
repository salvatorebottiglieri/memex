"""Shared fixtures and helpers for memex tests."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memex.store import Store


WORKTREE = Path(__file__).resolve().parent.parent


def _run_memex(args: list[str], cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    import shutil
    import subprocess
    import sys

    full_env = {**os.environ, **(env or {})}
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


def _store():
    con = sqlite3.connect(":memory:")
    s = Store(con)
    s.init_schema()
    return s


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_node(store: dict, vault_path: Path, filename: str, source_url: str) -> subprocess.CompletedProcess:
    """Write a markdown file with frontmatter and register it."""
    content = (
        f"---\nsource_url: {source_url}\ntitle: Test Article\n---\n\n"
        f"# Test Article\n\n"
        f"This is a longer article body that exceeds the minimum character threshold "
        f"of one hundred characters so that the L0 markdown file gets created in tests."
    )
    path = vault_path / filename
    path.write_text(content, encoding="utf-8")
    return _run_memex(
        ["register", "--db", str(store["db"]), "--vault", str(store["vault"]), str(path)],
        cwd=WORKTREE,
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

