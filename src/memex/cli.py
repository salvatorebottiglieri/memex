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


def _require_db(db_path: Path) -> None:
    """Exit with clean JSON error if the database file doesn't exist."""
    if not db_path.exists():
        click.echo(json.dumps({"error": "db_not_found", "db_path": str(db_path)}), err=True)
        raise SystemExit(1)


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


def _ingest_single_url(store, vault_path: Path, url: str, fetcher, *, source_name: str | None = None,
                       item_timestamp: str | None = None, item_note: str | None = None) -> dict:
    """Ingest one URL through the Store. Optionally records an inbox row first.

    Returns a dict suitable for JSON output (status, id, canonical_key, ...).
    """
    from memex.canonical_key import canonical_key
    from memex.fetcher import FetchError

    ckey = canonical_key(url)
    now = datetime.now(timezone.utc).isoformat

    # Record the inbox capture if this came from an inbox source
    if source_name is not None:
        store.add_inbox_item(
            source_name=source_name,
            url=url,
            timestamp=item_timestamp or now(),
            note=item_note,
            captured_at=now(),
        )

    # --- Ledger check ---
    existing = store.lookup_by_canonical_key(ckey)
    if existing is not None:
        return {
            "id": existing["node_id"],
            "status": "already_exists",
            "canonical_key": ckey,
            "failed": existing["failed"],
        }

    # --- Fetch content ---
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
        md_path = vault_path / f"{node_id}.md"
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
        created_at=now(),
    )
    store.attach_source(
        node_id=node_id,
        canonical_key=ckey,
        source_url=url,
        title=title,
        fetched_at=now(),
        failed=failed,
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
    from memex.store import Store
    from memex.whatsapp_source import parse_whatsapp_export

    if url is None and inbox_path is None:
        raise click.UsageError("Provide either a URL argument or --inbox <file>.")

    _require_db(db_path)
    fetcher = load_fetcher(os.environ.get("MEMEX_FETCHER_MODULE"))

    with Store.open(db_path) as store:
        if inbox_path is not None:
            source_name = f"whatsapp:{inbox_path}"
            export_text = inbox_path.read_text(encoding="utf-8")
            all_items = list(parse_whatsapp_export(export_text))

            # Read cursor — last processed message index (0-based)
            cursor_str = store.get_cursor(source_name)
            cursor_index = int(cursor_str) if cursor_str is not None else 0
            new_items = all_items[cursor_index:]

            results = [
                _ingest_single_url(
                    store, vault_path, item["url"], fetcher,
                    source_name=source_name,
                    item_timestamp=item["timestamp"],
                    item_note=item.get("note"),
                )
                for item in new_items
            ]

            # Advance cursor to end of all items seen (idempotent re-runs)
            store.set_cursor(source_name, str(len(all_items)))

            click.echo(json.dumps(results))
        else:
            click.echo(json.dumps(_ingest_single_url(store, vault_path, url, fetcher)))


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
    from memex.store import Store

    _require_db(db_path)
    with Store.open(db_path) as store:
        if show_pending:
            ingested = store.list_ingested_canonical_keys()
            pending, seen = [], set()
            for row in store.list_inbox():
                ckey = canonical_key(row["url"])
                if ckey not in ingested and ckey not in seen:
                    pending.append(ckey)
                    seen.add(ckey)
            click.echo(json.dumps(pending))
        else:
            click.echo(json.dumps(store.list_nodes()))


@cli.command()
@_db_options
@click.argument("node_id")
def show(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Return JSON with a node's content, metadata, trust state, and provenance (read-only)."""
    from memex.store import Store

    _require_db(db_path)
    with Store.open(db_path) as store:
        node = store.get_node(node_id)

    if node is None:
        click.echo(json.dumps({"error": "not_found", "id": node_id}), err=True)
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


@cli.command()
@_db_options
@click.argument("node_id")
def derive(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Generate a notes-tier derivation from an L0 node using an LLM.

    Writes derivation prose as <deriv_id>.md in the vault, inserts a node row
    (kind=summary, tier=notes, trust_state=draft, depth=1), records a derived_from
    provenance edge, and runs deterministic checks to transition draft → auto-verified.
    """
    from memex.checks import run_checks
    from memex.llm_client import load_llm_client
    from memex.store import Store

    _require_db(db_path)
    llm = load_llm_client(os.environ.get("MEMEX_LLM_MODULE"))

    with Store.open(db_path) as store:
        # --- Load the L0 node ---
        l0 = store.get_node(node_id)
        if l0 is None:
            click.echo(json.dumps({"error": "not_found", "id": node_id}), err=True)
            raise SystemExit(1)
        if not l0.get("content_path"):
            click.echo(json.dumps({"error": "no_content", "id": node_id}), err=True)
            raise SystemExit(1)

        # --- Idempotency check ---
        existing = store.find_derived_from(node_id)
        if existing is not None:
            click.echo(json.dumps({
                "id": existing["from_node"],
                "status": "already_derived",
                "l0_node_id": node_id,
            }))
            return

        l0_content = Path(l0["content_path"]).read_text(encoding="utf-8")
        deriv = llm.derive(l0_content)

        # --- Write derivation markdown file ---
        deriv_id = str(uuid.uuid4())
        vault_path.mkdir(parents=True, exist_ok=True)
        md_path = vault_path / f"{deriv_id}.md"
        md_path.write_text(deriv.prose, encoding="utf-8")

        # --- Insert derivation node + provenance edge ---
        now = datetime.now(timezone.utc).isoformat()
        store.create_node(
            node_id=deriv_id,
            kind="summary",
            tier="notes",
            trust_state="draft",
            depth=1,
            content_path=str(md_path),
            created_at=now,
        )
        store.create_edge(
            edge_id=str(uuid.uuid4()),
            type="provenance",
            relation="derived_from",
            from_node=deriv_id,
            to_node=node_id,
        )

        # --- Run deterministic checks → update trust_state ---
        check_result = run_checks(store._con, deriv_id, md_path)
        trust_state = "auto-verified" if check_result.passed else "draft"
        store.update_trust_state(
            node_id=deriv_id,
            trust_state=trust_state,
            check_failures=check_result.failures,
        )

    click.echo(json.dumps({
        "id": deriv_id,
        "status": "derived",
        "l0_node_id": node_id,
        "content_path": str(md_path),
        "trust_state": trust_state,
        "check_failures": check_result.failures,
    }))


@cli.command()
@_db_options
@click.argument("query")
def search(db_path: Path, vault_path: Path, query: str) -> None:
    """Keyword search over derivation content. Returns JSON array (read-only).

    Each result has: id, snippet, canonical_key, l0_node_id.
    """
    from memex.store import Store

    _require_db(db_path)
    CONTEXT_CHARS = 120
    query_lower = query.lower()

    with Store.open(db_path) as store:
        rows = store.list_edges(relation="derived_from", type="provenance")
        results = []
        for edge in rows:
            deriv_id = edge["from_node"]
            deriv = store.get_node(deriv_id)
            if deriv is None or not deriv.get("content_path"):
                continue
            p = Path(deriv["content_path"])
            if not p.exists():
                continue
            content = p.read_text(encoding="utf-8")
            if query_lower not in content.lower():
                continue

            idx = content.lower().find(query_lower)
            start = max(0, idx - CONTEXT_CHARS // 2)
            end = min(len(content), idx + len(query_lower) + CONTEXT_CHARS // 2)
            snippet = content[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."

            l0 = store.get_node(edge["to_node"])
            canonical_key = l0.get("canonical_key") if l0 else None

            results.append({
                "id": deriv_id,
                "snippet": snippet,
                "canonical_key": canonical_key,
                "l0_node_id": edge["to_node"],
            })

    click.echo(json.dumps(results))


@cli.command()
@_db_options
def render(db_path: Path, vault_path: Path) -> None:
    """Project SQLite graph into markdown frontmatter (ADR-0008).

    Reads every node, computes YAML frontmatter with metadata + tags + aliases,
    and writes it into the node's markdown file preserving the body.
    One-way DB → markdown. Idempotent.
    """
    from memex.renderer import render as _render

    _require_db(db_path)
    if not vault_path.exists():
        click.echo(json.dumps({"error": "vault_not_found", "vault_path": str(vault_path)}), err=True)
        raise SystemExit(1)

    results = _render(db_path, vault_path)
    click.echo(json.dumps(results))


if __name__ == "__main__":
    cli()
