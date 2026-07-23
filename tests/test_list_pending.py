"""Tests for `memex list --pending`.

Returns canonical keys captured in the inbox but not yet present in the
ledger. Persists across runs (derived from the inbox table, not re-parsing).

Pending state is reached when capture and ingestion are separate steps —
e.g., a future `memex capture` command, or when capture happens but the
ingestion step fails before the URL reaches the ledger. These tests exercise
that state by writing directly to the inbox table.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from tests.conftest import _run_memex, FAKE_FETCHER
from memex.canonical_key import canonical_key


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _list_pending(store) -> list:
    result = _run_memex(
        ["list", "--db", str(store["db"]), "--vault", str(store["vault"]), "--pending"],
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _ingest_url(store, url: str) -> None:
    result = _run_memex(
        ["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result.returncode == 0, result.stderr


def _capture_into_inbox(store, urls: list[str], source: str = "whatsapp:test") -> None:
    """Insert inbox rows directly (simulates a capture step that hasn't ingested yet)."""
    con = sqlite3.connect(store["db"])
    now = _now()
    for url in urls:
        con.execute(
            "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, url, "2024-06-01T09:00:00", None, now),
        )
    con.commit()
    con.close()


def test_list_pending_returns_empty_on_fresh_db(store):
    assert _list_pending(store) == []


def test_list_pending_returns_captured_but_not_ingested_keys(store):
    _capture_into_inbox(store, ["https://example.com/a", "https://example.com/b"])
    pending = _list_pending(store)
    assert set(pending) == {
        canonical_key("https://example.com/a"),
        canonical_key("https://example.com/b"),
    }


def test_list_pending_excludes_already_ingested_keys(store):
    _capture_into_inbox(store, ["https://example.com/a", "https://example.com/b"])
    _ingest_url(store, "https://example.com/a")
    pending = _list_pending(store)
    assert pending == [canonical_key("https://example.com/b")]


def test_list_pending_persists_across_runs(store):
    """Pending state survives process restart (derived from SQLite, not in-memory)."""
    _capture_into_inbox(store, ["https://persistent.example.com/x"])
    # A second CLI invocation reads from the same DB
    pending = _list_pending(store)
    assert canonical_key("https://persistent.example.com/x") in pending


def test_list_pending_dedupes_within_inbox(store):
    """Same URL captured twice yields one pending key."""
    _capture_into_inbox(store, [
        "https://example.com/article",
        "https://example.com/article?utm_source=x",
    ])
    pending = _list_pending(store)
    assert pending == [canonical_key("https://example.com/article")]


def test_list_pending_strips_tracking_params(store):
    _capture_into_inbox(store, ["https://blog.example.com/post?utm_source=twitter"])
    pending = _list_pending(store)
    assert pending == ["https://blog.example.com/post"]


def test_list_pending_output_is_json_array(store):
    _capture_into_inbox(store, ["https://example.com/x"])
    result = _run_memex(
        ["list", "--db", str(store["db"]), "--vault", str(store["vault"]), "--pending"],
    )
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(result.stdout), list)


def test_list_default_still_returns_nodes(store):
    """`list` (no flag) returns node rows, not pending keys."""
    _ingest_url(store, "https://example.com/article")
    result = _run_memex(
        ["list", "--db", str(store["db"]), "--vault", str(store["vault"])],
    )
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["canonical_key"] == "https://example.com/article"


def test_list_pending_empty_after_full_ingest(store):
    """When `ingest --inbox` runs (capture + ingest atomically), nothing is pending."""
    inbox_path = store["tmp"] / "inbox.txt"
    inbox_path.write_text(
        "[01/06/2024, 09:15:32] Alice: https://example.com/article\n",
        encoding="utf-8",
    )
    result = _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]),
         "--inbox", str(inbox_path)],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result.returncode == 0, result.stderr
    assert _list_pending(store) == []