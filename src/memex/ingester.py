"""Ingester — orchestrates the ingestion of a single URL into the ledger.

Houses ``ingest_single_url``, the core ingestion pipeline shared by
``memex ingest <url>``, ``memex ingest --inbox``, and
``memex ingest --from-inbox``. Extracted from cli.py for testability.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from memex.canonical_key import canonical_key
from memex.fetcher import FetchError


def _human_path(vault_path: Path, name: str, suffix: str = ".md") -> Path:
    """Return a collision-aware human-readable file path. Mirror of
    ``memex.cli._human_path``; duplicated here to avoid a circular import
    (cli -> ingester -> cli). Keep in sync if either changes.
    """
    base = vault_path / f"{_slugify(name)}{suffix}"
    if not base.exists():
        return base
    for i in range(1, 100):
        candidate = vault_path / f"{_slugify(name)}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    return base


def _slugify(text: str, max_length: int = 80) -> str:
    """Convert text to a filesystem-safe slug (lowercase, hyphens only)."""
    import re
    slug = text.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[-\s]+', '-', slug)
    slug = slug.strip('-')
    return slug[:max_length].rstrip('-')

def ingest_single_url(
    store,
    vault_path: Path,
    url: str,
    fetcher,
    *,
    source_name: str | None = None,
    item_timestamp: str | None = None,
    item_note: str | None = None,
) -> dict:
    """Ingest one URL through the Store. Optionally records an inbox row first.

    Args:
        store: An open Store instance.
        vault_path: Path to the vault directory.
        url: The URL to ingest.
        fetcher: A ContentFetcher-compatible object.
        source_name: If set, records an inbox row before ingesting.
        item_timestamp: Timestamp for the inbox row.
        item_note: Note for the inbox row.

    Returns:
        A dict with keys ``{id, status, canonical_key, ...}``
        suitable for JSON output.
    """
    ckey = canonical_key(url)
    now = datetime.now(timezone.utc).isoformat()

    # Record the inbox capture if this came from an inbox source
    if source_name is not None:
        store.add_inbox_item(
            source_name=source_name,
            url=url,
            timestamp=item_timestamp or now,
            note=item_note,
            captured_at=now,
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
        fetched_content_path = result.content_path
    except FetchError as exc:
        failed = True
        fetch_error_msg = str(exc)
        fetched_content_path = None


    # --- Write L0 markdown file (only on success, skip stubs < 100 chars) ---
    # The L0 file MUST live in the vault root (not in a hidden subfolder) so
    # Obsidian can index it and wikilinks from derivations resolve in the
    # graph view. When a fetcher provides its own cache path (YouTube,
    # PDF, etc.) we copy that file into the vault root and point
    # ``content_path`` at the copy. The original cache file is left in place
    # for debugging.
    content_path = ""
    if not failed and content is not None and len(content) >= 100:
        vault_path.mkdir(parents=True, exist_ok=True)
        name = _slugify(title) if title else node_id
        md_path = _human_path(vault_path, name)
        md_path.write_text(content, encoding="utf-8")
        content_path = str(md_path)
    elif fetched_content_path:
        # Fetcher cached the content outside the vault (e.g. .cache/youtube-*).
        # Mirror it into the vault root so Obsidian can index the L0.
        cache_file = Path(fetched_content_path)
        if cache_file.exists():
            vault_path.mkdir(parents=True, exist_ok=True)
            name = _slugify(title) if title else cache_file.stem
            md_path = _human_path(vault_path, name)
            md_path.write_text(cache_file.read_text(encoding="utf-8"), encoding="utf-8")
            content_path = str(md_path)
        else:
            content_path = fetched_content_path
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
