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

CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    url         TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    note        TEXT,
    captured_at TEXT NOT NULL
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

    # Migration: create inbox table if it does not exist yet (older DBs won't have it).
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS inbox (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            url         TEXT NOT NULL,
            timestamp   TEXT NOT NULL,
            note        TEXT,
            captured_at TEXT NOT NULL
        )
        """
    )
    con.commit()
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


def _ingest_url_core(
    con: sqlite3.Connection,
    vault_path: Path,
    url: str,
    fetcher,
) -> dict:
    """Ingest a single URL into an already-open DB connection.

    Returns a result dict (same shape as the ingest command's JSON output).
    Does NOT commit — caller is responsible for con.commit().
    """
    from memex.canonical_key import canonical_key
    from memex.fetcher import FetchError

    ckey = canonical_key(url)

    # --- Ledger check ---
    existing = con.execute(
        "SELECT node_id, failed FROM source WHERE canonical_key = ?", (ckey,)
    ).fetchone()
    if existing is not None:
        return {
            "id": existing[0],
            "status": "already_exists",
            "canonical_key": ckey,
            "failed": bool(existing[1]),
        }

    # --- Fetch content ---
    now = datetime.now(timezone.utc).isoformat()
    node_id = str(uuid.uuid4())
    failed = False
    fetch_error_msg = None
    content: str | None = None
    title: str | None = None

    try:
        fetch_result = fetcher.fetch(url)
        content = fetch_result.content
        title = fetch_result.title
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

    if failed:
        return {
            "id": node_id,
            "status": "fetch_failed",
            "canonical_key": ckey,
            "error": fetch_error_msg,
        }
    return {
        "id": node_id,
        "status": "ingested",
        "canonical_key": ckey,
        "title": title,
        "content_path": content_path,
    }


@cli.command()
@_db_options
@click.argument("url", required=False, default=None)
@click.option(
    "--inbox",
    "inbox_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a WhatsApp .txt export to ingest.",
)
def ingest(db_path: Path, vault_path: Path, url: str | None, inbox_path: Path | None) -> None:
    """Ingest a URL or a WhatsApp inbox export.

    Single URL:   memex ingest --db DB --vault V <url>
    WhatsApp file: memex ingest --db DB --vault V --inbox <file>

    Idempotent — running twice with the same (canonical) URL yields one node.
    A fetch failure is recorded and does not crash the run.
    """
    from memex.fetcher import load_fetcher

    if url is None and inbox_path is None:
        raise click.UsageError("Provide either a URL argument or --inbox <file>.")

    fetcher_module = os.environ.get("MEMEX_FETCHER_MODULE")
    fetcher = load_fetcher(fetcher_module)

    if inbox_path is not None:
        # --- WhatsApp inbox ingestion ---
        from memex.whatsapp_source import parse_whatsapp_export

        export_text = inbox_path.read_text(encoding="utf-8")
        source_name = f"whatsapp:{inbox_path}"

        con = sqlite3.connect(db_path)
        con.execute("PRAGMA foreign_keys = ON")

        # Read cursor — last processed message index (0-based)
        cursor_row = con.execute(
            "SELECT value FROM cursor WHERE source_name = ?", (source_name,)
        ).fetchone()
        cursor_index = int(cursor_row[0]) if cursor_row else 0

        # Parse all items from the export
        all_items = list(parse_whatsapp_export(export_text))

        # Only process items past the cursor
        new_items = all_items[cursor_index:]

        now = datetime.now(timezone.utc).isoformat()
        results = []

        for item in new_items:
            # Persist to inbox table
            con.execute(
                """
                INSERT INTO inbox (source_name, url, timestamp, note, captured_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_name, item["url"], item["timestamp"], item.get("note"), now),
            )
            # Ingest into ledger
            result = _ingest_url_core(con, vault_path, item["url"], fetcher)
            results.append(result)

        # Advance cursor to end of all items seen
        new_cursor = len(all_items)
        con.execute(
            "INSERT OR REPLACE INTO cursor (source_name, value) VALUES (?, ?)",
            (source_name, str(new_cursor)),
        )

        con.commit()
        con.close()
        click.echo(json.dumps(results))

    else:
        # --- Single URL ingestion ---
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA foreign_keys = ON")
        result = _ingest_url_core(con, vault_path, url, fetcher)
        con.commit()
        con.close()
        click.echo(json.dumps(result))


@cli.command("list")
@_db_options
@click.option(
    "--pending",
    "show_pending",
    is_flag=True,
    default=False,
    help="Return canonical keys captured from inbox but not yet ingested.",
)
def list_nodes(db_path: Path, vault_path: Path, show_pending: bool) -> None:
    """Return JSON array of all nodes, or --pending captured-but-not-ingested keys."""
    from memex.canonical_key import canonical_key

    con = sqlite3.connect(db_path)

    if show_pending:
        # Derive pending: inbox urls whose canonical_key is absent from source ledger
        inbox_rows = con.execute("SELECT url FROM inbox").fetchall()
        ingested_keys = {
            row[0]
            for row in con.execute("SELECT canonical_key FROM source").fetchall()
        }
        pending = []
        seen = set()
        for (url,) in inbox_rows:
            ckey = canonical_key(url)
            if ckey not in ingested_keys and ckey not in seen:
                pending.append(ckey)
                seen.add(ckey)
        con.close()
        click.echo(json.dumps(pending))
        return

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


@cli.command()
@_db_options
@click.argument("node_id")
def derive(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Generate a notes-tier derivation from an L0 node using an LLM.

    Writes derivation prose as <deriv_id>.md in the vault, inserts a node row
    (kind=summary, tier=notes, trust_state=draft, depth=1), and records a
    derived_from provenance edge in SQLite.
    """
    from memex.llm_client import load_llm_client

    llm_module = os.environ.get("MEMEX_LLM_MODULE")
    llm_client = load_llm_client(llm_module)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")

    # --- Load the L0 node ---
    row = con.execute(
        "SELECT content_path FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None:
        con.close()
        click.echo(json.dumps({"error": "not_found", "id": node_id}), err=False)
        raise SystemExit(1)

    content_path = row[0]
    if not content_path:
        con.close()
        click.echo(json.dumps({"error": "no_content", "id": node_id}), err=False)
        raise SystemExit(1)

    # --- Idempotency check ---
    existing_edge = con.execute(
        """
        SELECT from_node FROM edge
        WHERE to_node = ? AND type = 'provenance' AND relation = 'derived_from'
        LIMIT 1
        """,
        (node_id,),
    ).fetchone()
    if existing_edge is not None:
        con.close()
        click.echo(json.dumps({
            "id": existing_edge[0],
            "status": "already_derived",
            "l0_node_id": node_id,
        }))
        return

    l0_content = Path(content_path).read_text(encoding="utf-8")

    # --- Derive ---
    deriv_result = llm_client.derive(l0_content)

    # --- Write derivation markdown file ---
    deriv_id = str(uuid.uuid4())
    vault_path.mkdir(parents=True, exist_ok=True)
    md_path = vault_path / f"{deriv_id}.md"
    md_path.write_text(deriv_result.prose, encoding="utf-8")

    # --- Insert derivation node row ---
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at)
        VALUES (?, 'summary', 'notes', 'draft', 1, ?, ?)
        """,
        (deriv_id, str(md_path), now),
    )

    # --- Insert provenance edge ---
    edge_id = str(uuid.uuid4())
    con.execute(
        """
        INSERT INTO edge (id, type, relation, from_node, to_node)
        VALUES (?, 'provenance', 'derived_from', ?, ?)
        """,
        (edge_id, deriv_id, node_id),
    )

    con.commit()
    con.close()

    click.echo(json.dumps({
        "id": deriv_id,
        "status": "derived",
        "l0_node_id": node_id,
        "content_path": str(md_path),
    }))


@cli.command()
@_db_options
@click.argument("query")
def search(db_path: Path, vault_path: Path, query: str) -> None:
    """Keyword search over derivation content. Returns JSON array (read-only).

    Each result has: id, snippet, canonical_key, l0_node_id.
    """
    con = sqlite3.connect(db_path)

    # Find all derivation nodes (kind=summary, tier=notes) with content paths
    rows = con.execute(
        """
        SELECT n.id, n.content_path
        FROM node n
        WHERE n.kind = 'summary' AND n.tier = 'notes'
          AND n.content_path IS NOT NULL AND n.content_path != ''
        """
    ).fetchall()

    results = []
    query_lower = query.lower()

    for node_id, content_path in rows:
        p = Path(content_path)
        if not p.exists():
            continue
        content = p.read_text(encoding="utf-8")
        if query_lower not in content.lower():
            continue

        # Build snippet: find the line(s) containing the query
        snippet = _extract_snippet(content, query_lower)

        # Find the L0 node via provenance edge
        edge_row = con.execute(
            """
            SELECT to_node FROM edge
            WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
            """,
            (node_id,),
        ).fetchone()
        l0_node_id = edge_row[0] if edge_row else None

        # Get canonical_key from source table (the L0 source)
        canonical_key = None
        if l0_node_id:
            ck_row = con.execute(
                "SELECT canonical_key FROM source WHERE node_id = ?",
                (l0_node_id,),
            ).fetchone()
            if ck_row:
                canonical_key = ck_row[0]

        results.append({
            "id": node_id,
            "snippet": snippet,
            "canonical_key": canonical_key,
            "l0_node_id": l0_node_id,
        })

    con.close()
    click.echo(json.dumps(results))


def _extract_snippet(content: str, query_lower: str, context_chars: int = 120) -> str:
    """Extract a snippet from content around the first occurrence of the query."""
    idx = content.lower().find(query_lower)
    if idx == -1:
        return content[:context_chars]
    start = max(0, idx - context_chars // 2)
    end = min(len(content), idx + len(query_lower) + context_chars // 2)
    snippet = content[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet


if __name__ == "__main__":
    cli()
