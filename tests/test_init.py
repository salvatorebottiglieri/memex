"""Tests for `memex init` command."""
import json
import sqlite3
from pathlib import Path

import pytest


def test_init_outputs_json(tmp_path, run_memex):
    """memex init outputs valid JSON with db_path and vault_path (AXI standard)."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"

    result = run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["db_path"] == str(db_path)
    assert data["vault_path"] == str(vault_path)


def test_init_creates_sqlite_db_with_all_tables(tmp_path, run_memex):
    """memex init creates the SQLite DB with all four required tables."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"

    result = run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])

    assert result.returncode == 0, result.stderr
    assert db_path.exists(), "SQLite DB was not created"

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cur.fetchall()}
    con.close()

    assert tables == {"node", "source", "edge", "cursor"}


def test_init_creates_vault_directory(tmp_path, run_memex):
    """memex init creates the vault directory for markdown files."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"

    result = run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])

    assert result.returncode == 0, result.stderr
    assert vault_path.exists() and vault_path.is_dir(), "Vault directory was not created"


def test_init_is_idempotent(tmp_path, run_memex):
    """Running memex init twice yields the same state without error."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    args = ["init", "--db", str(db_path), "--vault", str(vault_path)]

    first = run_memex(args)
    second = run_memex(args)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    # DB still has all four tables after second run
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cur.fetchall()}
    con.close()

    assert tables == {"node", "source", "edge", "cursor"}
    assert vault_path.is_dir()
