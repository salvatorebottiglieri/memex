"""memex CLI — canonical agent-facing interface.

All output is JSON (AXI standard: structured, token-frugal, machine-readable).
"""
from __future__ import annotations

import dataclasses
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click

from memex.canonical_key import canonical_key
from memex.ingester import ingest_single_url
import functools

def _slugify(text: str, max_length: int = 80) -> str:
    """Convert text to a filesystem-safe slug (lowercase, hyphens only)."""
    import re
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    slug = slug.strip('-')
    return slug[:max_length].rstrip('-')


def _human_path(vault_path: Path, name: str, suffix: str = ".md") -> Path:
    """Return a human-readable file path, appending a suffix on collision."""
    base = vault_path / f"{_slugify(name)}{suffix}"
    if not base.exists():
        return base
    # Collision: append a short discriminator
    for i in range(1, 100):
        candidate = vault_path / f"{_slugify(name)}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    return base  # fallback (unlikely)


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
    """Fill in default db/vault from env, Obsidian detection, or fallback."""
    vp = Path(vault_path) if vault_path else (
        Path(os.environ["MEMEX_VAULT"]) if "MEMEX_VAULT" in os.environ else _detect_vault()
    )
    if vp is None:
        vp = Path.home() / "memex-vault"
    dp = Path(db_path) if db_path else (
        Path(os.environ["MEMEX_DB"]) if "MEMEX_DB" in os.environ else vp / ".memex" / "memex.db"
    )
    return dp, vp

def _fail(error: str, **kwargs: Any) -> None:
    """Emit a JSON error to stderr and exit with code 1."""
    click.echo(json.dumps({"error": error, **kwargs}), err=True)
    raise SystemExit(1)



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
    from memex.agent import load_agent
    from memex.fetcher import load_fetcher
    from memex.store import Store
    from memex.whatsapp_source import parse_whatsapp_export

    if not from_inbox and url is None and inbox_path is None:
        raise click.UsageError(
            "Provide a URL argument, --inbox <file>, or --from-inbox."
        )

    
    fetcher = load_fetcher(os.environ.get("MEMEX_FETCHER_MODULE"), vault_path=str(vault_path))

    title_agent = load_agent(os.environ.get("MEMEX_AGENT"))
 
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
                    if not result.get("title") and title_agent and result.get("status") == "ingested" and result.get("content_path"):
                        cp = Path(result["content_path"])
                        if cp.exists():
                            t = title_agent.generate_title(cp.read_text(encoding="utf-8"), item["url"])
                            if t:
                                store.update_source_title(result["id"], t)
                                result["title"] = t
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
                if not result.get("title") and title_agent and result.get("status") == "ingested" and result.get("content_path"):
                    cp = Path(result["content_path"])
                    if cp.exists():
                        t = title_agent.generate_title(cp.read_text(encoding="utf-8"), item["url"])
                        if t:
                            store.update_source_title(result["id"], t)
                            result["title"] = t
                results.append(result)
                click.echo(f"[{i}/{total}] {result.get('status','?')}  {item['url']}", err=True)

            # Advance cursor to end of all items seen (idempotent re-runs)
            store.set_cursor(source_name, str(len(all_items)))

            click.echo(json.dumps(results))
        else:
            # Pre-flight: attempt resolution for non-ingestable URLs
            from memex.fetcher import resolve_url
            resolution = resolve_url(url)
            if not resolution.ingestable:
                from memex.resolver import detect_resolver, ResolverError
                resolver = detect_resolver()
                if resolver is not None:
                    click.echo(json.dumps({"status": "resolving_via_agent", "url": url}), err=True)
                    try:
                        resolved = resolver.resolve(url)
                        result = ingest_single_url(store, vault_path, resolved, fetcher)
                        if not result.get("title") and title_agent and result.get("status") == "ingested" and result.get("content_path"):
                            cp = Path(result["content_path"])
                            if cp.exists():
                                t = title_agent.generate_title(cp.read_text(encoding="utf-8"), resolved)
                                if t:
                                    store.update_source_title(result["id"], t)
                                    result["title"] = t
                        click.echo(json.dumps(result))
                        return
                    except ResolverError as e:
                        _fail(str(e), url=url)
                else:
                    _fail(resolution.note or "Cannot ingest this URL", url=url)
            result = ingest_single_url(store, vault_path, url, fetcher)
            if not result.get("title") and title_agent and result.get("status") == "ingested" and result.get("content_path"):
                cp = Path(result["content_path"])
                if cp.exists():
                    t = title_agent.generate_title(cp.read_text(encoding="utf-8"), url)
                    if t:
                        store.update_source_title(result["id"], t)
                        result["title"] = t
            click.echo(json.dumps(result))


@cli.command()
@click.argument("url", required=False, default=None)
def resolve(url: str | None) -> None:
    """Resolve a URL through resolution rules and return JSON.

    Returns the type, ingestability, and direct_url (if applicable).
    """
    if not url:
        _fail("Missing required argument 'URL'.")
    from memex.fetcher import resolve_url
    result = resolve_url(url)
    click.echo(json.dumps(dataclasses.asdict(result)))


@cli.command()
@click.argument("url", required=False, default=None)
def resolve_agent(url: str | None) -> None:
    """Resolve a URL using an external agent (Pi/Claude) with a browser.

    Returns JSON with the resolved URL, or an error if no agent is available.
    """
    if not url:
        _fail("Missing required argument 'URL'.")
    from memex.resolver import detect_resolver, ResolverError
    resolver = detect_resolver()
    if resolver is None:
        _fail("No resolver agent available. Install pi or set MEMEX_RESOLVER_CMD.")
    try:
        resolved = resolver.resolve(url)
        click.echo(json.dumps({"resolved_url": resolved}))
    except ResolverError as e:
        _fail(str(e))


@cli.command("cookies-export")
@click.argument("domain", default="x.com")
@click.option("--output", "-o", default=None, help="Output file (default: stdout)")
def cookies_export(domain: str, output: str | None) -> None:
    """Export cookies for a domain (e.g. x.com) to use with resolve-agent.

    Opens a headless browser; login if needed, then cookies are saved.
    Compatible with MEMEX_COOKIES_FILE env var.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _fail("Playwright is required: pip install playwright && playwright install chromium")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            click.echo(f"Navigating to https://{domain}...", err=True)
            page.goto(f"https://{domain}", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            click.echo(f"Current URL: {page.url}", err=True)
            click.echo("If login is required, login now, then press Enter here...", err=True)
            input()
            page.wait_for_timeout(2000)
            cookies = ctx.cookies()
            browser.close()
            import json as _json
            data = _json.dumps(cookies, indent=2)
            if output:
                Path(output).write_text(data)
                click.echo(json.dumps({"status": "saved", "file": output, "count": len(cookies)}))
            else:
                click.echo(data)
    except Exception as e:
        _fail(str(e))


@cli.command("list")
@_db_options
@click.option(
    "--pending",
    "show_pending",
    is_flag=True,
    default=False,
    help="Return canonical keys captured from inbox but not yet ingested.",
)
@click.option("--kind", default=None, help="Filter by node kind (e.g. raw_source, summary).")
@click.option("--tier", default=None, help="Filter by node tier (e.g. notes, synthesis).")
@click.option("--trust-state", "trust_state", default=None, help="Filter by trust state (draft, auto-verified, human-approved, stale).")
@click.option("--confidence", default=None, help="Filter by confidence (high, medium, low).")
@click.option(
    "--synthesis-statement",
    "synthesis_statement",
    default=None,
    help="Substring match against any synthesis statement (uses the structured column).",
)
@click.option("--limit", default=None, type=int, help="Max results.")
@click.option("--offset", default=None, type=int, help="Result offset for pagination.")
def list_nodes(db_path: Path, vault_path: Path, show_pending: bool,
               kind: str | None, tier: str | None, trust_state: str | None,
               confidence: str | None, synthesis_statement: str | None,
               limit: int | None, offset: int | None) -> None:
    """Return JSON array of all nodes, or --pending captured-but-not-ingested keys."""
    from memex.store import Store


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
            results = store.list_nodes(
                kind=kind, tier=tier, trust_state=trust_state,
                confidence=confidence, limit=limit, offset=offset,
            )
            if synthesis_statement:
                needle = synthesis_statement.lower()
                results = [
                    n for n in results
                    if n.get("synthesis_statements") and any(
                        needle in s.lower()
                        for s in n["synthesis_statements"]
                    )
                ]
            click.echo(json.dumps(results))

@cli.command()
@_db_options
@click.argument("node_id")
def show(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Return JSON with a node's content, metadata, trust state, and provenance (read-only)."""
    from memex.store import Store

    
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
    provenance edge, and runs deterministic checks to transition draft -> auto-verified.
    """
    from memex.agent import load_agent
    from memex.store import Store

    

    if derive_all:
        _derive_all(db_path, vault_path, limit)
    else:
        if not node_id:
            _fail("missing_node_id", detail="Provide a node_id or use --all for batch mode.")
        _derive_single(db_path, vault_path, node_id)



@cli.command()
@_db_options
@click.argument("node_ids", nargs=-1, required=True)
def synthesize(db_path: Path, vault_path: Path, node_ids: tuple[str, ...]) -> None:
    """Generate a synthesis-tier derivation from one or more nodes using an LLM.

    Synthesizes across the given parent nodes, writing the result as a new markdown
    file in the vault, and creating a node with tier=synthesis and derived_from
    provenance edges back to each parent.

    Idempotent: calling synthesize with the same unordered set of parent IDs returns
    the existing synthesis without re-running the agent.

    Example: memex synthesize --db DB --vault V <id1> <id2> <id3>
    """
    from memex.agent import load_agent
    from memex.store import Store

    
    agent = load_agent(os.environ.get("MEMEX_AGENT"))

    parent_ids = list(node_ids)

    with Store.open(db_path) as store:
        # --- Validate all parent nodes exist ---
        for pid in parent_ids:
            parent = store.get_node(pid)
            if parent is None:
                _fail("not_found", node_id=pid)

        # --- Idempotency check: unordered parent set ---
        existing = store.find_synthesis_by_parents(parent_ids)
        if existing is not None:
            click.echo(json.dumps({
                "id": existing["id"],
                "status": "already_synthesized",
                "parent_ids": parent_ids,
            }))
            return

        # --- Run synthesis ---
        try:
            result = _do_synthesize(store, vault_path, parent_ids, agent)
        except Exception as exc:
            _fail("agent_failed", detail=str(exc))

    click.echo(json.dumps(result))

def _derive_single(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Derive a single L0 node (the original behavior)."""
    from memex.fetcher import load_fetcher
    from memex.agent import load_agent
    from memex.store import Store

    agent = load_agent(os.environ.get("MEMEX_AGENT"))

    with Store.open(db_path) as store:
        # --- Load the L0 node ---
        l0 = store.get_node(node_id)
        if l0 is None:
            _fail("not_found", id=node_id)

        # --- Load content (from file or fetch fresh) ---
        if l0.get("content_path") and Path(l0["content_path"]).exists():
            l0_content = Path(l0["content_path"]).read_text(encoding="utf-8")
        else:
            # L0 has no file — fetch URL fresh (may yield metadata-only content)
            try:
                fetcher = load_fetcher(os.environ.get("MEMEX_FETCHER_MODULE"), vault_path=str(vault_path))
                result = fetcher.fetch(l0["source_url"])
                l0_content = result.content
            except Exception as exc:
                click.echo(json.dumps({"status": "error", "reason": f"No content available for derivation: {exc}"}))
                return
        # --- Idempotency check ---
        existing = store.find_derived_from(node_id)
        if existing is not None:
            click.echo(json.dumps({
                "id": existing["from_node"],
                "status": "already_derived",
                "l0_node_id": node_id,
            }))
            return

        result = _do_derive(store, vault_path, node_id, l0_content, agent)

    click.echo(json.dumps(result))


def _do_derive(store, vault_path, l0_id, l0_content, agent, use_retry=False):
    """Run agent derivation, write markdown, create node+edge, run checks.

    Returns a result dict with status="derived" on success.
    Raises on agent failure (caller catches for batch mode).
    """
    from memex.checks import run_checks
    from memex.agent import call_with_retry

    deriv_fn = lambda: agent.derive(l0_content)
    deriv = call_with_retry(deriv_fn) if use_retry else agent.derive(l0_content)

    deriv_id = str(uuid.uuid4())
    vault_path.mkdir(parents=True, exist_ok=True)
    # Extract heading from prose for human-readable filename
    first_line = deriv.prose.split('\n')[0].strip()
    head_name = first_line.lstrip('# ').strip().strip('"').strip("'") or deriv_id
    md_path = _human_path(vault_path, head_name)
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
        synthesis_statements=deriv.synthesis_statements,
    )
    store.create_edge(
        edge_id=str(uuid.uuid4()),
        type="provenance",
        relation="derived_from",
        from_node=deriv_id,
        to_node=l0_id,
    )

    # Notes-tier with 1 parent → medium confidence
    store._con.execute(
        "UPDATE node SET confidence = 'medium' WHERE id = ?", (deriv_id,)
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
        "content_path": str(md_path),
        "check_failures": check_result.failures,
    }


def _do_synthesize(store, vault_path, parent_ids, agent):
    """Run agent synthesis across multiple parents, write markdown, create node+edges, run checks.

    Returns a result dict on success. Raises on agent failure.
    """
    from memex.checks import run_checks
    from memex.agent import call_with_retry

    # Load parent nodes and compute max depth
    max_depth = 0
    contents = []
    for pid in parent_ids:
        parent = store.get_node(pid)
        if parent is None:
            raise ValueError(f"parent node not found: {pid}")
        max_depth = max(max_depth, parent["depth"])
        content_path = parent.get("content_path") or ""
        if content_path and Path(content_path).exists():
            contents.append(Path(content_path).read_text(encoding="utf-8"))
        else:
            contents.append("")

    combined_content = "\n\n---\n\n".join(contents)
    deriv_fn = lambda: agent.derive(combined_content)
    deriv = call_with_retry(deriv_fn)

    deriv_id = str(uuid.uuid4())
    vault_path.mkdir(parents=True, exist_ok=True)
    # Extract heading from prose for human-readable filename
    first_line = deriv.prose.split('\n')[0].strip()
    head_name = first_line.lstrip('# ').strip().strip('"').strip("'") or deriv_id
    md_path = _human_path(vault_path, head_name)
    md_path.write_text(deriv.prose, encoding="utf-8")

    now = datetime.now(timezone.utc).isoformat()
    store.create_node(
        node_id=deriv_id,
        kind="summary",
        tier="synthesis",
        trust_state="draft",
        depth=max_depth + 1,
        content_path=str(md_path),
        created_at=now,
        synthesis_statements=deriv.synthesis_statements,
    )

    for pid in parent_ids:
        store.create_edge(
            edge_id=str(uuid.uuid4()),
            type="provenance",
            relation="derived_from",
            from_node=deriv_id,
            to_node=pid,
        )

    # Synthesis: confidence = min(parents' confidence)
    confidences = []
    for pid in parent_ids:
        p = store.get_node(pid)
        if p and p.get("confidence"):
            confidences.append(p["confidence"])
    if "low" in confidences:
        synth_conf = "low"
    elif "medium" in confidences:
        synth_conf = "medium"
    else:
        synth_conf = "low"
    store._con.execute(
        "UPDATE node SET confidence = ? WHERE id = ?", (synth_conf, deriv_id)
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
        "status": "synthesized",
        "parent_ids": list(parent_ids),
        "trust_state": trust_state,
        "content_path": str(md_path),
        "check_failures": check_result.failures,
    }


def _derive_all(db_path: Path, vault_path: Path, limit: int) -> None:
    """Derive all un-derived L0 nodes, up to limit."""
    from memex.agent import load_agent
    from memex.store import Store

    if limit <= 0:
        click.echo(json.dumps([]))
        return

    agent = load_agent(os.environ.get("MEMEX_AGENT"))

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
                result = _do_derive(store, vault_path, node["id"], l0_content, agent, use_retry=True)
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
    """Keyword search over derivation content and L0 metadata. Returns JSON array (read-only).

    Each result has: id, snippet, canonical_key, l0_node_id, match_type.
    """
    from memex.store import Store

    
    CONTEXT_CHARS = 120
    query_lower = query.lower()
    query_param = f"%{query}%"

    with Store.open(db_path) as store:
        # ── First pass: derivation content (file scan) ─────────
        # Index results by l0_node_id for dedup
        by_l0: dict[str, dict] = {}
        rows = store.list_edges(relation="derived_from", type="provenance")
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
            ckey = l0.get("canonical_key") if l0 else None
            l0_id = edge["to_node"]

            by_l0[l0_id] = {
                "id": deriv_id,
                "snippet": snippet,
                "canonical_key": ckey,
                "l0_node_id": l0_id,
                "match_type": "derivation",
            }

        # ── Second pass: L0 metadata (SQL) ────────────────────
        meta_rows = store._con.execute(
            """
            SELECT n.id, s.title, s.source_url, s.canonical_key
            FROM node n
            JOIN source s ON s.node_id = n.id
            WHERE s.title LIKE ? OR s.source_url LIKE ? OR s.canonical_key LIKE ?
            """,
            (query_param, query_param, query_param),
        ).fetchall()

        for row in meta_rows:
            nid = row["id"]
            # Determine which field matched
            match_field = "title"
            if query_lower in (row["source_url"] or "").lower():
                match_field = "url"
            elif query_lower in (row["canonical_key"] or "").lower():
                match_field = "key"

            if nid in by_l0:
                by_l0[nid]["match_type"] = "multiple"
            else:
                # New result — show matched metadata as snippet
                matched_val = row[match_field] or ""
                by_l0[nid] = {
                    "id": nid,
                    "snippet": matched_val,
                    "canonical_key": row["canonical_key"],
                    "l0_node_id": nid,
                    "match_type": match_field,
                }

    click.echo(json.dumps(list(by_l0.values())))


@cli.command()
@_db_options
@click.argument("node_id")
def extract(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Extract key ideas from a node. Uses LLM agent. Idempotent — re-run replaces ideas."""
    from memex.agent import load_agent
    from memex.store import Store
    from memex.fetcher import load_fetcher

    
    agent = load_agent(os.environ.get("MEMEX_AGENT"))

    with Store.open(db_path) as store:
        node = store.get_node(node_id)
        if node is None:
            click.echo(json.dumps({"error": "not_found"}))
            return

        # Load content (from file or fetch fresh)
        if node.get("content_path") and Path(node["content_path"]).exists():
            content = Path(node["content_path"]).read_text(encoding="utf-8")
        else:
            try:
                fetcher = load_fetcher(os.environ.get("MEMEX_FETCHER_MODULE"), vault_path=str(vault_path))
                result = fetcher.fetch(node["source_url"])
                content = result.content
            except Exception as exc:
                click.echo(json.dumps({"error": "no_content", "detail": str(exc)}))
                return

        try:
            ideas = agent.extract_ideas(content)
        except Exception as exc:
            click.echo(json.dumps({"error": "agent_failed", "detail": str(exc)}))
            return

        store.set_node_ideas(node_id, ideas)

    click.echo(json.dumps({
        "node_id": node_id,
        "ideas_count": len(ideas),
        "ideas": ideas,
    }))


@cli.command()
@_db_options
@click.argument("query", required=False, default="")
def ideas(db_path: Path, vault_path: Path, query: str) -> None:
    """Search across extracted ideas. Returns JSON array of matching ideas with node metadata.

    Empty query returns all ideas. No match returns [].
    """
    from memex.store import Store

    
    with Store.open(db_path) as store:
        # Check if node_idea table exists (pre-migration safety)
        try:
            results = store.search_ideas(query if query else "%")
        except Exception:
            results = []
    click.echo(json.dumps(results))


@cli.command()
@_db_options
def render(db_path: Path, vault_path: Path) -> None:
    """Project SQLite graph into markdown frontmatter (ADR-0008).

    Reads every node, computes YAML frontmatter with metadata + tags + aliases,
    and writes it into the node's markdown file preserving the body.
    One-way DB -> markdown. Idempotent.
    """
    from memex.renderer import render as _render

    
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
    from memex.agent import load_agent, call_with_retry
    from memex.store import Store

    
    agent = load_agent(os.environ.get("MEMEX_AGENT"))

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

                review_fn = lambda: agent.review(target_content, asserting_content, edge_payload)
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


@cli.command()
@_db_options
@click.argument("node_id")
@click.option("--cascade", is_flag=True, default=False, help="Remove node and all provenance descendants transitively.")
def delete(db_path: Path, vault_path: Path, node_id: str, cascade: bool) -> None:
    """Remove a node from the vault (logical delete). File .md is kept on disk.

    Use --cascade to also remove all provenance descendants transitively.
    """
    from memex.store import Store

    
    with Store.open(db_path) as store:
        result = store.delete_node(node_id, cascade=cascade)
    click.echo(json.dumps(result))


@cli.command()
@_db_options
@click.argument("node_id")
def retry(db_path: Path, vault_path: Path, node_id: str) -> None:
    """Re-fetch a failed source URL. Updates content on success."""
    from memex.store import Store
    from memex.fetcher import load_fetcher, FetchError

    

    with Store.open(db_path) as store:
        node = store.get_node(node_id)
        if node is None:
            click.echo(json.dumps({"error": "not_found"}))
            return

        source_url = node.get("source_url")
        if not source_url or not node.get("failed"):
            click.echo(json.dumps({"error": "not_failed"}))
            return

        fetcher = load_fetcher(os.environ.get("MEMEX_FETCHER_MODULE"), vault_path=str(vault_path))
        try:
            result = fetcher.fetch(source_url)
            content = result.content
            content_path = result.content_path
        except FetchError as exc:
            click.echo(json.dumps({"error": "fetch_failed", "detail": str(exc)}))
            return

        # Write content file with human-readable name
        vault_path.mkdir(parents=True, exist_ok=True)
        title = node.get("title")
        name = _slugify(title) if title else node_id
        md_path = _human_path(vault_path, name)
        md_path.write_text(content, encoding="utf-8")

        # Update node content_path and reset failed
        store._con.execute(
            "UPDATE node SET content_path = ? WHERE id = ?",
            (str(md_path), node_id),
        )
        store.reset_source_failed(node_id)

    click.echo(json.dumps({
        "status": "retried",
        "id": node_id,
        "content_path": str(md_path),
    }))

@cli.command("backfill-synthesis")
@_db_options
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would change without writing to the DB.",
)
def backfill_synthesis(db_path: Path, vault_path: Path, dry_run: bool) -> None:
    """Backfill the synthesis_statements column from '> Synthesis:' markers in
    derivation markdown files. Idempotent — skips nodes whose column is already
    populated. Use after upgrading a vault that pre-dates the column.
    """
    import re as _re
    from memex.store import Store

    marker_re = _re.compile(r"^>\s*\*{0,2}Synthesis:\*{0,2}\s*(.+)$", _re.M)

    with Store.open(db_path) as store:
        candidates = [
            n for n in store.list_nodes()
            if n["kind"] != "raw_source"
            and not n.get("synthesis_statements")
            and n.get("content_path")
            and Path(n["content_path"]).exists()
        ]
        results: list[dict] = []
        for n in candidates:
            text = Path(n["content_path"]).read_text(encoding="utf-8")
            stmts = marker_re.findall(text)
            entry = {
                "id": n["id"],
                "content_path": n["content_path"],
                "extracted": len(stmts),
                "preview": stmts[0][:80] if stmts else None,
            }
            if stmts and not dry_run:
                store._con.execute(
                    "UPDATE node SET synthesis_statements = ? WHERE id = ?",
                    (json.dumps(stmts), n["id"]),
                )
                entry["status"] = "updated"
            elif stmts:
                entry["status"] = "would_update"
            else:
                entry["status"] = "no_marker_found"
            results.append(entry)
        click.echo(json.dumps({
            "dry_run": dry_run,
            "scanned": len(candidates),
            "results": results,
        }))


@cli.command()
@_db_options
def stats(db_path: Path, vault_path: Path) -> None:
    """Return high-level vault statistics as JSON."""
    from memex.store import Store

    with Store.open(db_path) as store:
        click.echo(json.dumps(store.get_stats()))


@cli.command()
@_db_options
@click.option("--push/--no-push", default=True, help="Push to remote after committing (default: push)")
@click.option("--install-hooks", is_flag=True, help="Install git post-merge hook for auto-render on pull")
def sync(db_path: Path, vault_path: Path, push: bool, install_hooks: bool) -> None:
    """Commit vault state to git and optionally push."""
    if install_hooks:
        _install_sync_hooks(vault_path)
        return

    from memex.renderer import render
    import subprocess

    # 1. Render DB -> frontmatter
    results = render(db_path, vault_path)
    rendered = sum(1 for r in results if r["status"] == "rendered")

    # 2. Git add + commit — ponytail: subprocess for 3 calls, not a library
    r = subprocess.run(["git", "add", "-A"], cwd=vault_path, capture_output=True, text=True)
    if r.returncode != 0:
        _fail("git-add-failed", stderr=r.stderr)

    r = subprocess.run(["git", "commit", "-m", "sync"], cwd=vault_path, capture_output=True, text=True)
    committed = r.returncode == 0

    # 3. Optional push
    pushed = False
    if push and committed:
        r = subprocess.run(["git", "push"], cwd=vault_path, capture_output=True, text=True)
        if r.returncode != 0:
            _fail("git-push-failed", stderr=r.stderr)
        pushed = True

    click.echo(json.dumps({
        "rendered": rendered,
        "committed": committed,
        "pushed": pushed,
    }))


def _install_sync_hooks(vault_path: Path) -> None:
    """Write git post-merge hook that re-renders frontmatter on pull."""
    hooks_dir = vault_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "post-merge"
    # ponytail: absolute vault path so MEMEX_VAULT env var is optional
    hook.write_text(
        "#!/bin/sh\n"
        f'exec memex render --vault "{vault_path}"\n'
    )
    hook.chmod(0o755)
    click.echo(json.dumps({"hook_installed": str(hook)}))
if __name__ == "__main__":
    cli()
