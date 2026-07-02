"""Tests for the TelegramSource protocol and the memex capture command.

Tests use a fake Telegram source injected via MEMEX_TELEGRAM_SOURCE env var.
No real Telegram credentials needed.
"""
from __future__ import annotations

import json
import sqlite3

from tests.conftest import _run_memex

FAKE_TELEGRAM_SOURCE = "tests.fake_telegram_source:FakeTelegramSource"


def _capture(store, extra_env: dict | None = None) -> "subprocess.CompletedProcess":
    env = {"MEMEX_TELEGRAM_SOURCE": FAKE_TELEGRAM_SOURCE, **(extra_env or {})}
    return _run_memex(
        ["capture", "--db", str(store["db"]), "--vault", str(store["vault"])],
        env=env,
    )


class TestTelegramSourceUnit:
    """Unit tests for TelegramSource protocol and load_telegram_source (no subprocess)."""

    def test_load_telegram_source_imports_fake(self):
        """load_telegram_source with a valid module:Class returns an instance."""
        from memex.telegram_source import load_telegram_source
        source = load_telegram_source(FAKE_TELEGRAM_SOURCE)
        assert source is not None
        messages = source.capture()
        assert len(messages) > 0

    def test_load_telegram_source_raises_without_module(self):
        """load_telegram_source without module path raises ImportError."""
        from memex.telegram_source import load_telegram_source
        import pytest
        with pytest.raises(ImportError):
            load_telegram_source(None)

    def test_fake_telegram_source_returns_messages(self):
        """FakeTelegramSource returns the configured messages."""
        from memex.telegram_source import CapturedMessage
        from tests.fake_telegram_source import FakeTelegramSource

        source = FakeTelegramSource()
        messages = source.capture()
        assert len(messages) >= 1
        assert isinstance(messages[0], CapturedMessage)
        assert messages[0].url is not None
        assert messages[0].timestamp is not None

    def test_fake_telegram_source_custom_messages(self):
        """FakeTelegramSource accepts custom message list."""
        from memex.telegram_source import CapturedMessage
        from tests.fake_telegram_source import FakeTelegramSource

        custom = [
            CapturedMessage(url="https://custom.example/1", timestamp="2024-06-01T10:00:00", note="custom note"),
        ]
        source = FakeTelegramSource(messages=custom)
        assert source.capture() == custom


class TestCaptureCLI:
    """Integration tests for memex capture via subprocess (CLI seam)."""

    def test_capture_returns_json_array(self, store):
        """memex capture returns a JSON array of captured items."""
        result = _capture(store)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_capture_writes_inbox_rows(self, store):
        """Captured items are persisted to the inbox table."""
        _capture(store)
        con = sqlite3.connect(store["db"])
        rows = con.execute("SELECT url, note FROM inbox").fetchall()
        con.close()
        assert len(rows) >= 1
        # The default fake returns at least one message with a note
        urls = [r[0] for r in rows]
        assert any("example.com" in u for u in urls)
        # At least one row should have a note
        notes = [r[1] for r in rows if r[1] is not None]
        assert len(notes) >= 1

    def test_capture_advances_cursor(self, store):
        """Re-running capture produces no new items (cursor advanced)."""
        _capture(store)
        result = _capture(store)
        data = json.loads(result.stdout)
        assert data == []  # no new items

    def test_capture_source_name_stored(self, store):
        """Inbox rows have source_name = 'telegram:saved_messages'."""
        _capture(store)
        con = sqlite3.connect(store["db"])
        source_names = {
            r[0] for r in con.execute("SELECT source_name FROM inbox").fetchall()
        }
        con.close()
        assert "telegram:saved_messages" in source_names

    def test_capture_cursor_in_db(self, store):
        """Cursor is persisted in the cursor table after capture."""
        _capture(store)
        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT source_name, value FROM cursor WHERE source_name = ?",
            ("telegram:saved_messages",),
        ).fetchone()
        con.close()
        assert row is not None
        # Cursor should reflect the count of items processed
        assert int(row[1]) > 0

    def test_capture_list_pending_shows_captured(self, store):
        """After capture, list --pending shows the captured URLs."""
        _capture(store)
        result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"]), "--pending"],
        )
        data = json.loads(result.stdout)
        assert len(data) >= 1
        assert any("example.com" in k for k in data)


class TestCaptureErrors:
    """Error handling for memex capture."""

    def test_capture_no_source_configured_errors(self, store):
        """capture without MEMEX_TELEGRAM_SOURCE exits non-zero with clean error."""
        result = _run_memex(
            ["capture", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert result.returncode != 0
        data = json.loads(result.stderr)
        assert data.get("error") == "source_not_found"

    def test_capture_missing_db_errors(self, store):
        """capture with missing DB exits non-zero."""
        result = _run_memex(
            ["capture", "--db", str(store["tmp"] / "nope.db"), "--vault", str(store["vault"])],
            env={"MEMEX_TELEGRAM_SOURCE": FAKE_TELEGRAM_SOURCE},
        )
        assert result.returncode != 0
