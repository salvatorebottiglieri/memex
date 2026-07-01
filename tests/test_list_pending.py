"""Tests for `memex list --pending`.

Returns a JSON array of canonical keys that have been captured (present in the
inbox table) but whose canonical key is absent from the source ledger — i.e.,
captured but not yet ingested.

Persists across process restarts (derived from SQLite inbox table, not re-parsing).
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
[01/06/2024, 10:00:00] Bob: https://news.example.com/story
[02/06/2024, 08:00:00] Charlie: https://blog.example.com/post
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
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])
    return {"db": db_path, "vault": vault_path, "tmp": tmp_path}


@pytest.fixture()
def inbox_file(tmp_path):
    p = tmp_path / "whatsapp_export.txt"
    p.write_text(FIXTURE_EXPORT, encoding="utf-8")
    return p


def list_pending(store) -> subprocess.CompletedProcess:
    return run_memex(
        ["list", "--pending", "--db", str(store["db"]), "--vault", str(store["vault"])],
    )


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


def ingest_url(store, url: str) -> subprocess.CompletedProcess:
    return run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )


class TestListPendingEmpty:
    def test_list_pending_returns_empty_when_nothing_captured(self, store):
        result = list_pending(store)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == []

    def test_list_pending_returns_empty_after_all_ingested(self, store, inbox_file):
        ingest_inbox(store, inbox_file)
        result = list_pending(store)
        data = json.loads(result.stdout)
        assert data == []


class TestListPendingWithCapturedItems:
    def test_list_pending_returns_captured_but_not_ingested_keys(self, store, tmp_path):
        """After capturing from inbox but NOT ingesting (no ingest --inbox run),
        pending should reflect captured but not yet stored items.

        We test this by directly inserting into inbox table without ingesting.
        """
        from memex.canonical_key import canonical_key as ckey

        con = sqlite3.connect(store["db"])
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        source_name = "whatsapp:test"
        for url in ["https://example.com/a", "https://example.com/b"]:
            con.execute(
                "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) VALUES (?, ?, ?, ?, ?)",
                (source_name, url, "2024-06-01T09:00:00", None, now),
            )
        con.commit()
        con.close()

        result = list_pending(store)
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 2
        expected_keys = {ckey("https://example.com/a"), ckey("https://example.com/b")}
        assert set(data) == expected_keys

    def test_list_pending_excludes_already_ingested_keys(self, store, tmp_path):
        """Items in inbox that have been ingested should NOT appear in pending."""
        from datetime import datetime, timezone
        from memex.canonical_key import canonical_key as ckey

        con = sqlite3.connect(store["db"])
        now = datetime.now(timezone.utc).isoformat()
        source_name = "whatsapp:test"
        for url in ["https://example.com/a", "https://example.com/b"]:
            con.execute(
                "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) VALUES (?, ?, ?, ?, ?)",
                (source_name, url, "2024-06-01T09:00:00", None, now),
            )
        con.commit()
        con.close()

        # Ingest just one
        ingest_url(store, "https://example.com/a")

        result = list_pending(store)
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0] == ckey("https://example.com/b")

    def test_list_pending_survives_process_restart(self, store, inbox_file):
        """Pending items must survive process restart (derived from SQLite inbox table)."""
        # Capture from inbox but simulate not ingesting: insert into inbox directly
        from datetime import datetime, timezone
        con = sqlite3.connect(store["db"])
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) VALUES (?, ?, ?, ?, ?)",
            ("whatsapp:test", "https://persistent.example.com/x", "2024-06-01T09:00:00", None, now),
        )
        con.commit()
        con.close()

        # A new process run of list --pending should still see it
        result = list_pending(store)
        data = json.loads(result.stdout)
        from memex.canonical_key import canonical_key as ckey
        assert ckey("https://persistent.example.com/x") in data

    def test_list_pending_output_is_json_array(self, store):
        result = list_pending(store)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert isinstance(data, list)
