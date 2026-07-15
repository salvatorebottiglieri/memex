"""Tests for `memex init` command."""
import json
import sqlite3
from pathlib import Path

from tests.conftest import FAKE_FETCHER


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
    """memex init creates the SQLite DB with all required tables."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"

    result = run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])

    assert result.returncode == 0, result.stderr
    assert db_path.exists(), "SQLite DB was not created"

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cur.fetchall() if not row[0].startswith("sqlite_")}
    con.close()

    assert tables == {"node", "source", "edge", "cursor", "inbox", "event_queue", "event_node_link", "review_proposal", "node_idea"}


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

    # DB still has all required tables after second run
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row[0] for row in cur.fetchall() if not row[0].startswith("sqlite_")}
    con.close()

    assert tables == {"node", "source", "edge", "cursor", "inbox", "event_queue", "event_node_link", "review_proposal", "node_idea"}
    assert vault_path.is_dir()


def test_init_migrates_missing_failed_column_in_source(tmp_path, run_memex):
    """init adds the `failed` column to an existing source table that lacks it.

    Simulates an old-schema DB (from before issue-3) by creating the table
    without `failed`, then re-running init, then running ingest — asserting
    no crash and the column is present.
    """
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"

    # Create DB with old schema (source table without `failed` column)
    con = sqlite3.connect(db_path)
    con.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS node (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            tier         TEXT,
            trust_state  TEXT NOT NULL,
            depth        INTEGER NOT NULL,
            content_path TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source (
            node_id       TEXT PRIMARY KEY REFERENCES node(id),
            canonical_key TEXT NOT NULL UNIQUE,
            source_url    TEXT NOT NULL,
            title         TEXT,
            fetched_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS edge (
            id        TEXT PRIMARY KEY,
            type      TEXT NOT NULL,
            relation  TEXT NOT NULL,
            from_node TEXT NOT NULL REFERENCES node(id),
            to_node   TEXT NOT NULL REFERENCES node(id)
        );
        CREATE TABLE IF NOT EXISTS cursor (
            source_name TEXT PRIMARY KEY,
            value       TEXT NOT NULL
        );
    """)
    con.commit()
    con.close()
    vault_path.mkdir(parents=True, exist_ok=True)

    # Run init on the existing old-schema DB — should add the missing column
    result = run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])
    assert result.returncode == 0, result.stderr

    # ingest must not crash (OperationalError: no column named failed)
    result2 = run_memex(
        ["ingest", "--db", str(db_path), "--vault", str(vault_path), "https://example.com/article"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result2.returncode == 0, result2.stderr
    data = json.loads(result2.stdout)
    assert data["status"] == "ingested"

    # Confirm the column now exists
    con = sqlite3.connect(db_path)
    cols = [row[1] for row in con.execute("PRAGMA table_info(source)").fetchall()]
    con.close()
    assert "failed" in cols


def test_init_created_flags_reflect_actual_creation(tmp_path, run_memex):
    """db_created and vault_created are True on first run, False on second run."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    args = ["init", "--db", str(db_path), "--vault", str(vault_path)]

    first = run_memex(args)
    second = run_memex(args)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    first_data = json.loads(first.stdout)
    second_data = json.loads(second.stdout)

    assert first_data["db_created"] is True
    assert first_data["vault_created"] is True
    assert second_data["db_created"] is False
    assert second_data["vault_created"] is False


def test_resolve_paths_uses_env_vars(tmp_path, run_memex):
    """MEMEX_VAULT and MEMEX_DB env vars are picked up by _resolve_paths."""
    vault = tmp_path / "my-vault"
    vault.mkdir()
    db = tmp_path / "custom.db"
    db.write_text("")  # placeholder so init is safe

    env = {"MEMEX_VAULT": str(vault), "MEMEX_DB": str(db)}
    result = run_memex(["status"], env=env)

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["vault_path"] == str(vault)
    assert data["db_path"] == str(db)