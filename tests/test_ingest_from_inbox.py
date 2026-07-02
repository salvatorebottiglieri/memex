"""Tests for `memex ingest --from-inbox`.

Reads all rows from the inbox table whose canonical key is not yet in the
source ledger and ingests each URL via `_ingest_single_url`. Idempotent.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from tests.conftest import _run_memex, FAKE_FETCHER, ingest


def _from_inbox(store) -> list:
    """Run `memex ingest --from-inbox` and return parsed JSON."""
    result = _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), "--from-inbox"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _add_inbox_item(store, url: str, note: str | None = None) -> None:
    """Insert an inbox row directly (simulates a capture that hasn't ingested yet)."""
    con = sqlite3.connect(store["db"])
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("test:direct", url, "2024-06-01T09:00:00", note, now),
    )
    con.commit()
    con.close()


# ── Basic functionality ──────────────────────────────────────────


class TestFromInbox:
    def test_from_inbox_ingests_urls(self, store):
        """Inbox items that are not in the ledger get ingested."""
        _add_inbox_item(store, "https://example.com/article")
        results = _from_inbox(store)
        assert len(results) == 1
        assert results[0]["status"] in ("ingested",)

    def test_from_inbox_creates_nodes(self, store):
        """Each ingested inbox item creates a node row."""
        _add_inbox_item(store, "https://example.com/a")
        _add_inbox_item(store, "https://example.com/b")
        _from_inbox(store)

        con = sqlite3.connect(store["db"])
        nodes = con.execute("SELECT kind, depth FROM node").fetchall()
        con.close()
        kinds = {r[0] for r in nodes}
        assert kinds == {"raw_source"}

    def test_from_inbox_returns_json_array(self, store):
        """Output is a JSON array with one entry per ingested URL."""
        _add_inbox_item(store, "https://example.com/a")
        _add_inbox_item(store, "https://example.com/b")
        results = _from_inbox(store)
        assert isinstance(results, list)
        assert len(results) == 2

    def test_from_inbox_idempotent(self, store):
        """Re-running with the same inbox is idempotent — URLs already ingested."""
        _add_inbox_item(store, "https://example.com/article")
        first = _from_inbox(store)
        assert len(first) == 1
        assert first[0]["status"] == "ingested"

        second = _from_inbox(store)
        assert len(second) == 1
        assert second[0]["status"] == "already_exists"

    def test_from_inbox_does_not_duplicate_nodes(self, store):
        """Re-running does not create duplicate node rows."""
        _add_inbox_item(store, "https://example.com/article")
        _from_inbox(store)
        _from_inbox(store)

        con = sqlite3.connect(store["db"])
        node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        con.close()
        assert node_count == 1


# ── Edge cases ───────────────────────────────────────────────────


class TestFromInboxEdgeCases:
    def test_from_inbox_empty_vault(self, store):
        """Empty inbox returns empty JSON array."""
        results = _from_inbox(store)
        assert results == []

    def test_from_inbox_deduplicates_by_canonical_key(self, store):
        """Two inbox rows with the same canonical key produce one node."""
        _add_inbox_item(store, "https://example.com/article")
        _add_inbox_item(store, "https://example.com/article?utm_source=x")
        results = _from_inbox(store)
        # Two result entries: first ingests, second is already_exists
        assert len(results) == 2
        assert results[0]["status"] == "ingested"
        assert results[1]["status"] == "already_exists"

        con = sqlite3.connect(store["db"])
        node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        con.close()
        assert node_count == 1

    def test_from_inbox_mixed_ingested_and_new(self, store):
        """Already-ingested URLs are skipped; new ones are ingested."""
        # Directly ingest one URL first
        result = ingest(store, "https://example.com/alpha")
        assert result.returncode == 0, result.stderr

        # Now add inbox items — one already ingested, one new
        _add_inbox_item(store, "https://example.com/alpha")  # already in ledger
        _add_inbox_item(store, "https://example.com/beta")   # new

        results = _from_inbox(store)
        assert len(results) == 2
        statuses = {r["status"] for r in results}
        assert statuses == {"already_exists", "ingested"}

        con = sqlite3.connect(store["db"])
        node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        con.close()
        assert node_count == 2

    def test_from_inbox_does_not_delete_inbox_rows(self, store):
        """Inbox rows are preserved after ingest (append-only inbox)."""
        _add_inbox_item(store, "https://example.com/article")
        _from_inbox(store)

        con = sqlite3.connect(store["db"])
        inbox_count = con.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        con.close()
        assert inbox_count == 1

    def test_from_inbox_fetch_failure(self, store):
        """A failing fetch is recorded and does not crash."""
        _add_inbox_item(store, "https://fail.example.com/bad")
        results = _from_inbox(store)
        assert len(results) == 1
        assert results[0]["status"] == "fetch_failed"

        con = sqlite3.connect(store["db"])
        row = con.execute("SELECT failed FROM source").fetchone()
        con.close()
        assert row[0] == 1


# ── Integration with existing commands ───────────────────────────


class TestFromInboxIntegration:
    def test_from_inbox_list_pending_clears(self, store):
        """After --from-inbox, `list --pending` returns empty."""
        _add_inbox_item(store, "https://example.com/article")
        _from_inbox(store)

        result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"]), "--pending"],
        )
        data = json.loads(result.stdout)
        assert data == []

    def test_from_inbox_strips_tracking_params(self, store):
        """Ingestion uses canonical_key so tracking params are stripped."""
        _add_inbox_item(store, "https://example.com/article?utm_source=twitter")
        results = _from_inbox(store)
        assert len(results) == 1
        assert results[0]["canonical_key"] == "https://example.com/article"

    def test_from_inbox_preserves_note(self, store):
        """Adding an inbox item with a note works (note not used by ingest but preserved)."""
        _add_inbox_item(store, "https://example.com/article", note="interesting read")
        _from_inbox(store)

        con = sqlite3.connect(store["db"])
        row = con.execute("SELECT note FROM inbox").fetchone()
        con.close()
        assert row[0] == "interesting read"


# ── Smoke-compatible quick checks ────────────────────────────────


def test_from_inbox_multiple_urls(store):
    """Multiple URLs of different types all get ingested."""
    _add_inbox_item(store, "https://example.com/alpha")
    _add_inbox_item(store, "https://example.com/beta")
    _add_inbox_item(store, "https://example.com/gamma")

    results = _from_inbox(store)
    assert len(results) == 3
    assert all(r["status"] == "ingested" for r in results)

    con = sqlite3.connect(store["db"])
    node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    con.close()
    assert node_count == 3
