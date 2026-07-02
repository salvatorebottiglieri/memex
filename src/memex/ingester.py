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
