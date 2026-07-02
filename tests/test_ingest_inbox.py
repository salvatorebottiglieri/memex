"""Tests for `memex ingest --inbox <file>` (WhatsApp export ingestion).

Inbox ingestion persists captures to the inbox table, then ingests each
URL through the ledger. Cursor advances so re-runs don't re-ingest.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.conftest import _run_memex


FIXTURE_EXPORT = """\
[01/06/2024, 09:15:32] Alice: https://example.com/article
[01/06/2024, 10:00:00] Bob: Check this out https://news.example.com/story interesting read
[02/06/2024, 08:00:00] Bob: https://blog.example.com/post?utm_source=twitter
[02/06/2024, 09:00:00] Alice: Morning!
"""


def _ingest_inbox(store, inbox_text: str) -> "subprocess.CompletedProcess":  # type: ignore[name-defined]
    inbox_path = store["tmp"] / "inbox.txt"
    inbox_path.write_text(inbox_text, encoding="utf-8")
    return _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]),
         "--inbox", str(inbox_path)],
        env={"MEMEX_FETCHER_MODULE": "tests.conftest:FakeFetcher"},
    )


def test_ingest_inbox_returns_json_array(store):
    result = _ingest_inbox(store, FIXTURE_EXPORT)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    # 3 URLs in the fixture (Morning! has no link)
    assert len(data) == 3


def test_ingest_inbox_creates_nodes_for_each_url(store):
    _ingest_inbox(store, FIXTURE_EXPORT)
    con = sqlite3.connect(store["db"])
    rows = con.execute("SELECT kind FROM node").fetchall()
    con.close()
    kinds = {r[0] for r in rows}
    assert kinds == {"raw_source"}


def test_ingest_inbox_persists_to_inbox_table(store):
    _ingest_inbox(store, FIXTURE_EXPORT)
    con = sqlite3.connect(store["db"])
    rows = con.execute("SELECT url, note FROM inbox ORDER BY id").fetchall()
    con.close()
    assert len(rows) == 3
    urls = [r[0] for r in rows]
    assert "https://example.com/article" in urls
    # Note on the Bob story line: "Check this out  interesting read" (url stripped, spaces collapsed)
    bob_row = next(r for r in rows if "news.example.com" in r[0])
    assert bob_row[1] is not None
    assert "interesting read" in bob_row[1]


def test_ingest_inbox_advances_cursor(store):
    """Re-running the same inbox file is a no-op (cursor moved past)."""
    _ingest_inbox(store, FIXTURE_EXPORT)
    # second run with same content
    result = _ingest_inbox(store, FIXTURE_EXPORT)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    # Cursor moved past all 3 items → nothing to process
    assert data == []

    con = sqlite3.connect(store["db"])
    node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    inbox_count = con.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    con.close()
    assert node_count == 3  # no duplicates
    assert inbox_count == 3  # inbox rows are also deduplicated by cursor advance


def test_ingest_inbox_records_fetch_failures(store):
    """A URL that fails to fetch still gets a row in inbox + source with failed=1."""
    failing_export = "[01/06/2024, 09:15:32] Alice: https://fail.example.com/bad\n"
    result = _ingest_inbox(store, failing_export)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["status"] == "fetch_failed"

    con = sqlite3.connect(store["db"])
    row = con.execute("SELECT failed FROM source").fetchone()
    con.close()
    assert row[0] == 1


def test_ingest_inbox_deduplicates_by_canonical_key(store):
    """Same URL repeated in the export yields one node."""
    export = (
        "[01/06/2024, 09:15:32] Alice: https://example.com/article\n"
        "[01/06/2024, 09:16:00] Alice: https://example.com/article?utm_source=x\n"
    )
    result = _ingest_inbox(store, export)
    assert result.returncode == 0, result.stderr
    con = sqlite3.connect(store["db"])
    node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    con.close()
    assert node_count == 1