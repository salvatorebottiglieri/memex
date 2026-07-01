"""memex CLI — canonical agent-facing interface.

All output is JSON (AXI standard: structured, token-frugal, machine-readable).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click


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
    from memex.store import Store

    db_existed = db_path.exists()
    vault_existed = vault_path.exists()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with Store.open(db_path) as store:
        store.init_schema()

    vault_path.mkdir(parents=True, exist_ok=True)

    click.echo(json.dumps({
        "db_path": str(db_path),
        "vault_path": str(vault_path),
        "db_created": not db_existed,
        "vault_created": not vault_existed,
    }))


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

    from memex.store import Store

    ckey = canonical_key(url)

    with Store.open(db_path) as store:
        # --- Ledger check ---
        existing = store.lookup_by_canonical_key(ckey)
        if existing is not None:
            click.echo(json.dumps({
                "id": existing["node_id"],
                "status": "already_exists",
                "canonical_key": ckey,
                "failed": existing["failed"],
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

        # --- Insert node + source rows ---
        store.create_node(
            node_id=node_id,
            kind="raw_source",
            trust_state="draft",
            depth=0,
            content_path=content_path,
            created_at=now,
        )
        store.attach_source(
            node_id=node_id,
            canonical_key=ckey,
            source_url=url,
            title=title,
            fetched_at=now,
            failed=failed,
        )

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
    from memex.store import Store

    with Store.open(db_path) as store:
        click.echo(json.dumps(store.list_nodes()))


@cli.command()
@_db_options
@click.argument("node_id")
def show(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Return JSON with a node's content, metadata, trust state, and provenance (read-only)."""
    from memex.store import Store

    with Store.open(db_path) as store:
        node = store.get_node(node_id)

    if node is None:
        click.echo(json.dumps({"error": "not_found", "id": node_id}), err=False)
        raise SystemExit(1)

    # Load file content (stays in CLI — ADR-0008: markdown owns content)
    content = None
    if node.get("content_path"):
        p = Path(node["content_path"])
        if p.exists():
            content = p.read_text(encoding="utf-8")

    node["content"] = content
    node["l0_path"] = node.pop("content_path", None) or None
    click.echo(json.dumps(node))


if __name__ == "__main__":
    cli()
