"""memex CLI — canonical agent-facing interface.

All output is JSON (AXI standard: structured, token-frugal, machine-readable).
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS node (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    tier         TEXT,
    trust_state  TEXT NOT NULL CHECK (trust_state IN ('draft','auto-verified','human-approved','stale')),
    depth        INTEGER NOT NULL,
    content_path TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source (
    node_id       TEXT PRIMARY KEY REFERENCES node(id),
    canonical_key TEXT NOT NULL UNIQUE,
    source_url    TEXT NOT NULL,
    title         TEXT,
    fetched_at    TEXT,
    failed        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edge (
    id        TEXT PRIMARY KEY,
    type      TEXT NOT NULL CHECK (type IN ('provenance','association')),
    relation  TEXT NOT NULL CHECK (relation IN ('derived_from','related','contradicts','refines')),
    from_node TEXT NOT NULL REFERENCES node(id),
    to_node   TEXT NOT NULL REFERENCES node(id)
);

CREATE TABLE IF NOT EXISTS cursor (
    source_name TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


def _db_options(fn):
    fn = click.option(
        "--db",
        "db_path",
        required=True,
        type=click.Path(dir_okay=False, path_type=Path),
        help="Path to the SQLite database file.",
    )(fn)
    fn = click.option(
        "--vault",
        "vault_path",
        required=True,
        type=click.Path(file_okay=False, path_type=Path),
        help="Path to the vault directory for markdown files.",
    )(fn)
    return fn


@click.group()
def cli() -> None:
    """memex — personal second-brain CLI."""


@cli.command()
@_db_options
def init(db_path: Path, vault_path: Path) -> None:
    """Create the SQLite DB and vault directory (idempotent)."""
    db_existed = db_path.exists()
    vault_existed = vault_path.exists()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA_SQL)
    con.commit()
    # Migration: add `failed` column to source table if it does not exist yet
    # (CREATE TABLE IF NOT EXISTS is a no-op on existing DBs, so ALTER TABLE is needed).
    try:
        con.execute("ALTER TABLE source ADD COLUMN failed INTEGER NOT NULL DEFAULT 0")
        con.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    con.close()

    vault_path.mkdir(parents=True, exist_ok=True)

    result = {
        "db_path": str(db_path),
        "vault_path": str(vault_path),
        "db_created": not db_existed,
        "vault_created": not vault_existed,
    }
    click.echo(json.dumps(result))


@cli.command()
@_db_options
def status(db_path: Path, vault_path: Path) -> None:
    """Return JSON with paths and existence flags."""
    result = {
        "db_path": str(db_path),
        "vault_path": str(vault_path),
        "db_exists": db_path.exists(),
        "vault_exists": vault_path.exists(),
    }
    click.echo(json.dumps(result))


@cli.command()
@_db_options
@click.argument("url")
def ingest(db_path: Path, vault_path: Path, url: str) -> None:
    """Ingest a URL: fetch, store L0 markdown, insert node+source rows.

    Idempotent — running twice with the same (canonical) URL yields one node.
    A fetch failure is recorded and does not crash the run.
    """
    from memex.canonical_key import canonical_key
    from memex.fetcher import FetchError, load_fetcher

    fetcher_module = os.environ.get("MEMEX_FETCHER_MODULE")
    fetcher = load_fetcher(fetcher_module)

    ckey = canonical_key(url)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")

    # --- Ledger check ---
    existing = con.execute(
        "SELECT node_id, failed FROM source WHERE canonical_key = ?", (ckey,)
    ).fetchone()
    if existing is not None:
        con.close()
        click.echo(json.dumps({
            "id": existing[0],
            "status": "already_exists",
            "canonical_key": ckey,
            "failed": bool(existing[1]),
        }))
        return

    # --- Fetch content ---
    now = datetime.now(timezone.utc).isoformat()
    node_id = str(uuid.uuid4())
    failed = False
    fetch_error_msg = None
    content: str | None = None
    title: str | None = None

    try:
        result = fetcher.fetch(url)
        content = result.content
        title = result.title
    except FetchError as exc:
        failed = True
        fetch_error_msg = str(exc)

    # --- Write L0 markdown file (only on success) ---
    content_path = ""
    if not failed and content is not None:
        vault_path.mkdir(parents=True, exist_ok=True)
        md_filename = f"{node_id}.md"
        md_path = vault_path / md_filename
        # L0 files are immutable — write once, never overwrite
        if not md_path.exists():
            md_path.write_text(content, encoding="utf-8")
        content_path = str(md_path)

    # --- Insert node row ---
    con.execute(
        """
        INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at)
        VALUES (?, 'raw_source', NULL, 'draft', 0, ?, ?)
        """,
        (node_id, content_path, now),
    )

    # --- Insert source row ---
    con.execute(
        """
        INSERT INTO source (node_id, canonical_key, source_url, title, fetched_at, failed)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (node_id, ckey, url, title, now, 1 if failed else 0),
    )

    con.commit()
    con.close()

    if failed:
        click.echo(
            json.dumps(
                {
                    "id": node_id,
                    "status": "fetch_failed",
                    "canonical_key": ckey,
                    "error": fetch_error_msg,
                }
            )
        )
    else:
        click.echo(
            json.dumps(
                {
                    "id": node_id,
                    "status": "ingested",
                    "canonical_key": ckey,
                    "title": title,
                    "content_path": content_path,
                }
            )
        )


@cli.command("list")
@_db_options
def list_nodes(db_path: Path, vault_path: Path) -> None:
    """Return JSON array of all nodes (read-only)."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        """
        SELECT n.id, n.kind, n.tier, n.trust_state, s.canonical_key
        FROM node n
        LEFT JOIN source s ON s.node_id = n.id
        ORDER BY n.created_at
        """
    ).fetchall()
    con.close()

    nodes = [
        {
            "id": row[0],
            "kind": row[1],
            "tier": row[2],
            "trust_state": row[3],
            "canonical_key": row[4],
        }
        for row in rows
    ]
    click.echo(json.dumps(nodes))


@cli.command()
@_db_options
@click.argument("node_id")
def show(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Return JSON with a node's content, metadata, trust state, and provenance (read-only)."""
    con = sqlite3.connect(db_path)
    row = con.execute(
        """
        SELECT
            n.id, n.kind, n.tier, n.trust_state, n.depth, n.content_path, n.created_at,
            s.canonical_key, s.source_url, s.title, s.fetched_at, s.failed
        FROM node n
        LEFT JOIN source s ON s.node_id = n.id
        WHERE n.id = ?
        """,
        (node_id,),
    ).fetchone()
    con.close()

    if row is None:
        click.echo(json.dumps({"error": "not_found", "id": node_id}), err=False)
        raise SystemExit(1)

    (
        nid, kind, tier, trust_state, depth, content_path, created_at,
        canonical_key, source_url, title, fetched_at, failed,
    ) = row

    # Load file content
    content = None
    if content_path:
        p = Path(content_path)
        if p.exists():
            content = p.read_text(encoding="utf-8")

    result = {
        "id": nid,
        "kind": kind,
        "tier": tier,
        "trust_state": trust_state,
        "depth": depth,
        "created_at": created_at,
        "content": content,
        "canonical_key": canonical_key,
        "source_url": source_url,
        "title": title,
        "fetched_at": fetched_at,
        "failed": bool(failed) if failed is not None else False,
        "l0_path": content_path or None,
    }
    click.echo(json.dumps(result))


if __name__ == "__main__":
    cli()
