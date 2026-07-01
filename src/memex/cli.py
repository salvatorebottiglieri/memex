"""memex CLI — canonical agent-facing interface.

All output is JSON (AXI standard: structured, token-frugal, machine-readable).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import click

SCHEMA_SQL = """
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
"""


@click.group()
def cli() -> None:
    """memex — personal second-brain CLI."""


@cli.command()
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to the SQLite database file.",
)
@click.option(
    "--vault",
    "vault_path",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Path to the vault directory for markdown files.",
)
def init(db_path: Path, vault_path: Path) -> None:
    """Create the SQLite DB and vault directory (idempotent)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_SQL)
    con.commit()
    con.close()

    vault_path.mkdir(parents=True, exist_ok=True)

    result = {
        "db_path": str(db_path),
        "vault_path": str(vault_path),
        "db_created": True,
        "vault_created": True,
    }
    click.echo(json.dumps(result))


@cli.command()
@click.option(
    "--db",
    "db_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to the SQLite database file.",
)
@click.option(
    "--vault",
    "vault_path",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Path to the vault directory for markdown files.",
)
def status(db_path: Path, vault_path: Path) -> None:
    """Return JSON with paths and existence flags."""
    result = {
        "db_path": str(db_path),
        "vault_path": str(vault_path),
        "db_exists": db_path.exists(),
        "vault_exists": vault_path.exists(),
    }
    click.echo(json.dumps(result))


if __name__ == "__main__":
    cli()
