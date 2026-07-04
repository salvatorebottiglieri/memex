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

from memex.canonical_key import canonical_key
from memex.ingester import ingest_single_url
import functools


_DEFAULT_VAULT: Path | None = None
_OBSIDIAN_CANDIDATES = [
    "notes/notes",
    "Obsidian",
    "Documents/Obsidian",
    "vault",
    "notes",
]

def _detect_vault() -> Path | None:
    """Find the Obsidian vault root by scanning for ``.obsidian/``."""
    global _DEFAULT_VAULT
    if _DEFAULT_VAULT is not None:
        return _DEFAULT_VAULT
    for rel in _OBSIDIAN_CANDIDATES:
        p = Path.home() / rel
        if (p / ".obsidian").is_dir():
            _DEFAULT_VAULT = p
            return p
    return None


def _resolve_paths(db_path, vault_path):
    """Fill in default db/vault from Obsidian detection or fallback."""
    vp = Path(vault_path) if vault_path else _detect_vault()
    if vp is None:
        vp = Path.home() / "memex-vault"
    dp = Path(db_path) if db_path else vp / ".memex" / "memex.db"
    return dp, vp

def _fail(error: str, **kwargs: Any) -> None:
    """Emit a JSON error to stderr and exit with code 1."""
    click.echo(json.dumps({"error": error, **kwargs}), err=True)
    raise SystemExit(1)


def _require_db(db_path: Path) -> None:
    """Exit with clean JSON error if the database file doesn't exist."""


@click.group()
def cli() -> None:
    """memex — personal second-brain CLI."""


def _db_options(fn):
    fn = click.option(
        "--db",
        "db_path",
        default=None,
        type=click.Path(dir_okay=False, path_type=Path),
        help="Path to the SQLite database file (default: <vault>/.memex/memex.db).",
    )(fn)
    fn = click.option(
        "--vault",
        "vault_path",
        default=None,
        type=click.Path(file_okay=False, path_type=Path),
        help="Path to the vault directory (default: auto-detected Obsidian vault, or ~/memex-vault).",
    )(fn)
    @click.pass_context
    @functools.wraps(fn)
    def wrapper(ctx, **kwargs):
        kwargs["db_path"], kwargs["vault_path"] = _resolve_paths(
            kwargs.get("db_path"), kwargs.get("vault_path")
        )
        ctx.params["db_path"] = kwargs["db_path"]
        ctx.params["vault_path"] = kwargs["vault_path"]
        return fn(**kwargs)
    return wrapper


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
@click.argument("url", required=False, default=None)
@click.option(
    "--inbox",
    "inbox_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a WhatsApp .txt export to ingest.",
)
@click.option(
    "--from-inbox",
    "from_inbox",
    is_flag=True,
    default=False,
    help="Ingest all pending inbox items (captured but not yet in the ledger).",
)
def ingest(db_path: Path, vault_path: Path, url: str | None, inbox_path: Path | None,
           from_inbox: bool) -> None:
    """Ingest a URL, a WhatsApp inbox export, or pending inbox items.

    Single URL:   memex ingest --db DB --vault V <url>
    WhatsApp file: memex ingest --db DB --vault V --inbox <file>
    Inbox flush:  memex ingest --db DB --vault V --from-inbox

    Idempotent — running twice with the same (canonical) URL yields one node.
    A fetch failure is recorded and does not crash the run.
    """
    from memex.fetcher import load_fetcher
    from memex.store import Store
    from memex.whatsapp_source import parse_whatsapp_export

    if not from_inbox and url is None and inbox_path is None:
        raise click.UsageError(
            "Provide a URL argument, --inbox <file>, or --from-inbox."
        )

    _require_db(db_path)
    fetcher = load_fetcher(os.environ.get("MEMEX_FETCHER_MODULE"))

    with Store.open(db_path) as store:
        if from_inbox:
            # ── From-inbox: ingest all pending inbox items ───────
            ingested_keys = store.list_ingested_canonical_keys()
            inbox_items = list(store.list_inbox())
            total = len(inbox_items)
            results = []
            for i, item in enumerate(inbox_items, start=1):
                ckey = canonical_key(item["url"])
                if ckey not in ingested_keys:
                    result = ingest_single_url(store, vault_path, item["url"], fetcher)
                    results.append(result)
                    if result.get("canonical_key"):
                        ingested_keys.add(result["canonical_key"])
                else:
                    existing = store.lookup_by_canonical_key(ckey)
                    result = {
                        "id": existing["node_id"] if existing else None,
                        "status": "already_exists",
                        "canonical_key": ckey,
                    }
                    results.append(result)
                click.echo(f"[{i}/{total}] {result.get('status','?')}  {item['url']}", err=True)
            click.echo(json.dumps(results))
        elif inbox_path is not None:
            source_name = f"whatsapp:{inbox_path}"
            export_text = inbox_path.read_text(encoding="utf-8")
            all_items = list(parse_whatsapp_export(export_text))

            # Read cursor — last processed message index (0-based)
            cursor_str = store.get_cursor(source_name)
            cursor_index = int(cursor_str) if cursor_str is not None else 0
            new_items = all_items[cursor_index:]

            total = len(new_items)
            results = []
            for i, item in enumerate(new_items, start=1):
                result = ingest_single_url(
                    store, vault_path, item["url"], fetcher,
                    source_name=source_name,
                    item_timestamp=item["timestamp"],
                    item_note=item.get("note"),
                )
                results.append(result)
                click.echo(f"[{i}/{total}] {result.get('status','?')}  {item['url']}", err=True)

            # Advance cursor to end of all items seen (idempotent re-runs)
            store.set_cursor(source_name, str(len(all_items)))

            click.echo(json.dumps(results))
        else:
            click.echo(json.dumps(ingest_single_url(store, vault_path, url, fetcher)))


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
        _fail("not_found", id=node_id)

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
@click.argument("node_id", required=False)
@click.option("--all", "derive_all", is_flag=True, default=False, help="Derive all un-derived L0 nodes.")
@click.option("--limit", "limit", default=10, type=int, help="Max derivations per run (default 10).")
def derive(db_path: Path, vault_path: Path, node_id: str | None = None,
           derive_all: bool = False, limit: int = 10) -> None:
    """Generate a notes-tier derivation from an L0 node using an LLM.

    Single node:  memex derive --db DB --vault V <node-id>
    Batch:        memex derive --db DB --vault V --all [--limit N]

    Writes derivation prose as <deriv_id>.md in the vault, inserts a node row
    (kind=summary, tier=notes, trust_state=draft, depth=1), records a derived_from
    provenance edge, and runs deterministic checks to transition draft → auto-verified.
    """
    from memex.llm_client import load_llm_client
    from memex.store import Store

    _require_db(db_path)

    if derive_all:
        _derive_all(db_path, vault_path, limit)
    else:
        if not node_id:
            _fail("missing_node_id", detail="Provide a node_id or use --all for batch mode.")
        _derive_single(db_path, vault_path, node_id)


def _derive_single(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Derive a single L0 node (the original behavior)."""
    from memex.llm_client import load_llm_client
    from memex.store import Store

    llm = load_llm_client(os.environ.get("MEMEX_LLM_MODULE"))

    with Store.open(db_path) as store:
        # --- Load the L0 node ---
        l0 = store.get_node(node_id)
        if l0 is None:
            _fail("not_found", id=node_id)
        if not l0.get("content_path"):
            _fail("no_content", id=node_id)

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
        result = _do_derive(store, vault_path, node_id, l0_content, llm)

    click.echo(json.dumps(result))


def _do_derive(store, vault_path, l0_id, l0_content, llm, use_retry=False):
    """Run LLM derivation, write markdown, create node+edge, run checks.

    Returns a result dict with status="derived" on success.
    Raises on LLM failure (caller catches for batch mode).
    """
    from memex.checks import run_checks
    from memex.llm_client import call_with_retry

    deriv_fn = lambda: llm.derive(l0_content)
    deriv = call_with_retry(deriv_fn) if use_retry else llm.derive(l0_content)

    deriv_id = str(uuid.uuid4())
    vault_path.mkdir(parents=True, exist_ok=True)
    md_path = vault_path / f"{deriv_id}.md"
    md_path.write_text(deriv.prose, encoding="utf-8")

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
        to_node=l0_id,
    )

    check_result = run_checks(store._con, deriv_id, md_path)
    trust_state = "auto-verified" if check_result.passed else "draft"
    store.update_trust_state(
        node_id=deriv_id,
        trust_state=trust_state,
        check_failures=check_result.failures,
    )

    return {
        "id": deriv_id,
        "status": "derived",
        "l0_node_id": l0_id,
        "trust_state": trust_state,
        "check_failures": check_result.failures,
    }


def _derive_all(db_path: Path, vault_path: Path, limit: int) -> None:
    """Derive all un-derived L0 nodes, up to limit."""
    from memex.llm_client import load_llm_client
    from memex.store import Store

    if limit <= 0:
        click.echo(json.dumps([]))
        return

    llm = load_llm_client(os.environ.get("MEMEX_LLM_MODULE"))

    with Store.open(db_path) as store:
        # Find un-derived L0s
        all_nodes = store.list_nodes()
        un_derived = []
        for node in all_nodes:
            if node.get("kind") != "raw_source":
                continue
            if store.find_derived_from(node["id"]) is not None:
                continue
            un_derived.append(node["id"])
            if len(un_derived) >= limit:
                break

        results = []

        # Report already-derived L0s
        seen_derived = set()
        for node in all_nodes:
            if node.get("kind") != "raw_source":
                continue
            existing = store.find_derived_from(node["id"])
            if existing is not None:
                results.append({
                    "id": node["id"],
                    "status": "already_derived",
                })
                seen_derived.add(node["id"])

        # Derive un-derived L0s
        count = 0
        for node in all_nodes:
            if node.get("kind") != "raw_source":
                continue
            if node["id"] in seen_derived:
                continue
            if count >= limit:
                break
            count += 1

            l0 = store.get_node(node["id"])
            if l0 is None or not l0.get("content_path"):
                continue

            try:
                l0_content = Path(l0["content_path"]).read_text(encoding="utf-8")
                result = _do_derive(store, vault_path, node["id"], l0_content, llm, use_retry=True)
                results.append(result)
            except Exception as e:
                results.append({
                    "id": node["id"],
                    "status": "error",
                    "detail": str(e),
                })

    click.echo(json.dumps(results))


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
        _fail("vault_not_found", vault_path=str(vault_path))

    results = _render(db_path, vault_path)
    click.echo(json.dumps(results))


@cli.command()
@_db_options
def capture(db_path: Path, vault_path: Path) -> None:
    """Poll Telegram Saved Messages and persist new captures to the inbox.

    Reads new messages from the configured Telegram source
    (MEMEX_TELEGRAM_API_ID + MEMEX_TELEGRAM_API_HASH, or MEMEX_TELEGRAM_SOURCE),
    writes each to the inbox table, and advances the cursor.
    Idempotent — re-running only processes messages after the last cursor position.

    First run: Telethon will prompt for phone number + 2FA code interactively.
    Subsequent runs reuse the session file (default: ~/.memex/telegram.session,
    override via MEMEX_TELEGRAM_SESSION).
    """
    from memex.telegram_source import (
        load_telegram_source, CapturedMessage,
        CredentialsError, AuthFailedError, NetworkError,
    )
    from memex.store import Store

    _require_db(db_path)
    source_module = os.environ.get("MEMEX_TELEGRAM_SOURCE")
    try:
        source = load_telegram_source(source_module)
    except CredentialsError as e:
        _fail("missing_credentials", detail=str(e))
    except ImportError as e:
        _fail("source_not_found", detail=str(e))

    source_name = "telegram:saved_messages"

    with Store.open(db_path) as store:
        cursor_str = store.get_cursor(source_name)
        cursor = int(cursor_str) if cursor_str is not None else None

        try:
            messages = source.capture(cursor=cursor)
        except AuthFailedError as e:
            _fail("auth_failed", detail=str(e))
        except NetworkError as e:
            _fail("network_error", detail=str(e))

        now = datetime.now(timezone.utc).isoformat()
        results = []
        for msg in messages:
            store.add_inbox_item(
                source_name=source_name,
                url=msg.url,
                timestamp=msg.timestamp,
                note=msg.note,
                captured_at=now,
            )
            results.append({"url": msg.url, "timestamp": msg.timestamp, "note": msg.note})

        # Advance cursor to the highest Telegram message ID seen
        if messages:
            store.set_cursor(source_name, str(max(msg.id for msg in messages if msg.id)))

    click.echo(json.dumps(results))


@cli.group(invoke_without_command=True)
@_db_options
@click.pass_context
def review(ctx: click.Context, db_path: Path, vault_path: Path) -> None:
    """Review pending contestation events and manage proposals.

    Without a subcommand: batch-generate proposals for all pending events
    that don't already have one. Each event invokes the LLM with the
    target (contested) node content and the asserting edge's source node
    content, then persists the resulting ReviewProposal.

    Subcommands:
        list  — show the full review queue (pending events + proposals).
    """
    if ctx.invoked_subcommand is not None:
        return
    _cmd_review_batch(db_path, vault_path)


def _cmd_review_batch(db_path: Path, vault_path: Path) -> None:
    """Batch-generate proposals for all pending events without proposals."""
    from memex.llm_client import load_llm_client, call_with_retry
    from memex.store import Store

    _require_db(db_path)
    llm = load_llm_client(os.environ.get("MEMEX_LLM_MODULE"))

    with Store.open(db_path) as store:
        events = store.get_pending_events_without_proposal()
        results = []

        for event in events:
            try:
                target_node = store.get_node(event["target_node_id"])
                if target_node is None or not target_node.get("content_path"):
                    results.append({
                        "event_id": event["id"],
                        "status": "error",
                        "detail": "target_node_not_found",
                    })
                    continue

                # Find the asserting node (from_node of the contradicts edge)
                edge_rows = store._con.execute(
                    "SELECT from_node FROM edge WHERE id = ?", (event["edge_id"],)
                ).fetchone()
                if edge_rows is None:
                    results.append({
                        "event_id": event["id"],
                        "status": "error",
                        "detail": "edge_not_found",
                    })
                    continue

                asserting_node_id = edge_rows["from_node"]
                asserting_node = store.get_node(asserting_node_id)
                if asserting_node is None or not asserting_node.get("content_path"):
                    results.append({
                        "event_id": event["id"],
                        "status": "error",
                        "detail": "asserting_node_not_found",
                    })
                    continue

                target_content = Path(target_node["content_path"]).read_text(encoding="utf-8")
                asserting_content = Path(asserting_node["content_path"]).read_text(encoding="utf-8")
                edge_payload = {"edge_id": event["edge_id"]}

                review_fn = lambda: llm.review(target_content, asserting_content, edge_payload)
                proposal = call_with_retry(review_fn)

                proposal_id = store.write_review_proposal(
                    event_id=event["id"],
                    affected_node_ids=proposal.affected_node_ids,
                    damage_boundary_node_id=proposal.damage_boundary_node_id,
                    rationale_md=proposal.rationale_md,
                    confidence=proposal.confidence,
                )
                results.append({
                    "event_id": event["id"],
                    "proposal_id": proposal_id,
                    "status": "proposed",
                })
            except Exception as e:
                results.append({
                    "event_id": event["id"],
                    "status": "error",
                    "detail": str(e),
                })

    click.echo(json.dumps({"processed": len(events), "proposals": results}))


@review.command(name="list")
@click.pass_context
def review_list(ctx: click.Context) -> None:
    """Return JSON list of the review queue (pending events + pending proposals)."""
    from memex.store import Store

    db_path = ctx.parent.params["db_path"]
    vault_path = ctx.parent.params["vault_path"]
    _require_db(db_path)
    with Store.open(db_path) as store:
        queue = store.get_review_queue()
    click.echo(json.dumps(queue))


@review.command(name="accept")
@click.pass_context
@click.argument("proposal_id", type=int)
@click.option("--note", default=None, help="Optional human note.")
def review_accept(ctx: click.Context, proposal_id: int, note: str | None) -> None:
    """Accept a review proposal — mark affected nodes as stale, close event."""
    from memex.store import Store
    db_path = ctx.parent.params["db_path"]
    vault_path = ctx.parent.params["vault_path"]
    _require_db(db_path)
    with Store.open(db_path) as store:
        result = store.accept_proposal(proposal_id, human_note=note)
    click.echo(json.dumps(result))


@review.command(name="reject")
@click.pass_context
@click.argument("proposal_id", type=int)
@click.option("--note", default=None, help="Optional human note.")
def review_reject(ctx: click.Context, proposal_id: int, note: str | None) -> None:
    """Reject a review proposal — close event, no trust_state changes."""
    from memex.store import Store
    db_path = ctx.parent.params["db_path"]
    vault_path = ctx.parent.params["vault_path"]
    _require_db(db_path)
    with Store.open(db_path) as store:
        result = store.reject_proposal(proposal_id, human_note=note)
    click.echo(json.dumps(result))


@review.command(name="dismiss")
@click.pass_context
@click.argument("proposal_id", type=int)
@click.option("--note", default=None, help="Optional human note.")
def review_dismiss(ctx: click.Context, proposal_id: int, note: str | None) -> None:
    """Dismiss a review proposal — close event, no trust_state changes."""
    from memex.store import Store
    db_path = ctx.parent.params["db_path"]
    vault_path = ctx.parent.params["vault_path"]
    _require_db(db_path)
    with Store.open(db_path) as store:
        result = store.dismiss_proposal(proposal_id, human_note=note)
    click.echo(json.dumps(result))




@cli.command()
@_db_options
@click.argument("target_id")
@click.option(
    "--asserted-by",
    required=True,
    help="Node id that asserts the contradiction.",
)
def contradict(db_path: Path, vault_path: Path, target_id: str, asserted_by: str) -> None:
    """Write a ``contradicts`` edge targeting a node.

    The edge is written with ``written_by='human'``. The propagation
    (event_queue + contested on target + descendants) happens atomically
    inside ``create_edge``.

    Output JSON: ``{edge_id, target_node_id, asserted_by, written_by}``.
    """
    import uuid

    from memex.store import Store

    _require_db(db_path)
    edge_id = str(uuid.uuid4())
    with Store.open(db_path) as store:
        store.create_edge(
            edge_id=edge_id,
            type="association",
            relation="contradicts",
            from_node=asserted_by,
            to_node=target_id,
            written_by="human",
        )
    click.echo(json.dumps({
        "edge_id": edge_id,
        "target_node_id": target_id,
        "asserted_by": asserted_by,
        "written_by": "human",
    }))
if __name__ == "__main__":
    cli()
