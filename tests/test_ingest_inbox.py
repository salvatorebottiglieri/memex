"""Tests for `memex ingest --inbox <file>`.

Acceptance criteria exercised here:
- Parses a WhatsApp .txt export and ingests all URLs found
- Non-link messages are silently ignored
- Timestamps and adjacent notes are captured in inbox table
- Cursor advances after each run; re-runs process only new items
- Running ingest --inbox twice over the same export produces no duplicates
- Tests use a fixture WhatsApp export and the FakeFetcher (no real network)
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

FAKE_FETCHER = "tests.fake_fetcher:FakeFetcher"
WORKTREE = Path("/home/sbottiglieri/memex-issue-4")

FIXTURE_EXPORT = """\
[01/06/2024, 09:15:32] Alice: https://example.com/article
[01/06/2024, 10:00:00] Bob: Check this out https://news.example.com/story interesting read
[01/06/2024, 11:30:45] Alice: Just catching up, no links here
[02/06/2024, 08:00:00] Bob: https://blog.example.com/post?utm_source=twitter
"""

EXTENDED_FIXTURE = FIXTURE_EXPORT + """\
[03/06/2024, 07:00:00] Alice: https://extended.example.com/new-article
"""


def run_memex(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "memex.cli"] + args,
        capture_output=True,
        text=True,
        cwd=WORKTREE,
        env=full_env,
    )


@pytest.fixture()
def store(tmp_path):
    """Initialised db + vault."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])
    return {"db": db_path, "vault": vault_path, "tmp": tmp_path}


@pytest.fixture()
def inbox_file(tmp_path):
    """Write the fixture WhatsApp export to a temp file."""
    p = tmp_path / "whatsapp_export.txt"
    p.write_text(FIXTURE_EXPORT, encoding="utf-8")
    return p


def ingest_inbox(store, inbox_file: Path) -> subprocess.CompletedProcess:
    return run_memex(
        [
            "ingest",
            "--inbox", str(inbox_file),
            "--db", str(store["db"]),
            "--vault", str(store["vault"]),
        ],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )


class TestIngestInboxHappyPath:
    def test_ingest_inbox_exits_zero(self, store, inbox_file):
        result = ingest_inbox(store, inbox_file)
        assert result.returncode == 0, result.stderr

    def test_ingest_inbox_returns_json_array(self, store, inbox_file):
        result = ingest_inbox(store, inbox_file)
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_all_urls_ingested(self, store, inbox_file):
        result = ingest_inbox(store, inbox_file)
        data = json.loads(result.stdout)
        # Fixture has 3 URLs (1 non-link message ignored)
        assert len(data) == 3

    def test_each_item_has_status(self, store, inbox_file):
        result = ingest_inbox(store, inbox_file)
        data = json.loads(result.stdout)
        for item in data:
            assert "status" in item

    def test_each_item_has_canonical_key(self, store, inbox_file):
        result = ingest_inbox(store, inbox_file)
        data = json.loads(result.stdout)
        for item in data:
            assert "canonical_key" in item

    def test_nodes_are_stored_in_db(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        con = sqlite3.connect(store["db"])
        count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        con.close()
        assert count == 3

    def test_non_link_message_not_ingested(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        con = sqlite3.connect(store["db"])
        rows = con.execute("SELECT source_url FROM source").fetchall()
        con.close()
        urls = [r[0] for r in rows]
        assert not any("catching up" in u for u in urls)


class TestIngestInboxDeduplication:
    def test_running_twice_over_same_export_produces_no_duplicates(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        ingest_inbox(store, inbox_file)
        con = sqlite3.connect(store["db"])
        count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        con.close()
        assert count == 3

    def test_second_run_returns_already_exists_for_all(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        result = ingest_inbox(store, inbox_file)
        data = json.loads(result.stdout)
        statuses = [item["status"] for item in data]
        assert all(s == "already_exists" for s in statuses)


class TestIngestInboxCursor:
    def test_cursor_is_stored_after_first_run(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT value FROM cursor WHERE source_name LIKE 'whatsapp:%'"
        ).fetchone()
        con.close()
        assert row is not None

    def test_second_run_with_new_messages_ingests_only_new(self, store, tmp_path):
        """When the export grows with new messages, only new ones are ingested."""
        inbox_file = tmp_path / "wa.txt"
        inbox_file.write_text(FIXTURE_EXPORT, encoding="utf-8")

        ingest_inbox(store, inbox_file)
        # Now extend the export
        inbox_file.write_text(EXTENDED_FIXTURE, encoding="utf-8")
        result = ingest_inbox(store, inbox_file)

        data = json.loads(result.stdout)
        # Only the one new URL should be processed
        assert len(data) == 1
        assert data[0]["status"] == "ingested"

    def test_second_run_with_no_new_messages_returns_empty(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        result = ingest_inbox(store, inbox_file)
        # cursor already at end — no new messages to process means zero new items
        # (second run may return already_exists for items already ingested,
        # but with cursor it should skip them entirely)
        # The cursor points past all processed messages, so result should be empty
        data = json.loads(result.stdout)
        assert data == []


class TestIngestInboxPersistence:
    def test_inbox_table_records_captured_items(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        con = sqlite3.connect(store["db"])
        rows = con.execute("SELECT url, timestamp FROM inbox").fetchall()
        con.close()
        assert len(rows) == 3

    def test_inbox_table_records_note(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT note FROM inbox WHERE url LIKE '%news.example.com%'"
        ).fetchone()
        con.close()
        assert row is not None
        assert row[0] == "Check this out interesting read"
