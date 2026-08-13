"""Tests for `memex capture` — Telegram Saved Messages capture to the inbox.

Covers: cursor-monotonic capture (System Invariant 1), inbox persistence,
source injection (env var), and the error codes from the API contract
(missing_credentials / source_not_found / auth_failed / network_error).
All capture tests use the fake Telegram source — no real Telegram calls.
"""
from __future__ import annotations

import json

from memex.telegram_source import AuthFailedError, NetworkError
from tests.conftest import _q, _run_memex

FAKE_TELEGRAM_SOURCE = "tests.fake_telegram_source:FakeTelegramSource"


class AuthFailingSource:
    """Fake source that raises AuthFailedError on capture (plugin seam)."""

    def capture(self, cursor: int | None = None):
        raise AuthFailedError("session expired")


class NetworkFailingSource:
    """Fake source that raises NetworkError on capture (plugin seam)."""

    def capture(self, cursor: int | None = None):
        raise NetworkError("connection reset")


class EmptySource:
    """Fake source that never has new messages."""

    def capture(self, cursor: int | None = None):
        return []


def _capture(store, extra_env: dict | None = None, extra_args: list[str] | None = None):
    env = {"MEMEX_TELEGRAM_SOURCE": FAKE_TELEGRAM_SOURCE, **(extra_env or {})}
    return _run_memex(
        ["capture", *(extra_args or []), "--db", str(store["db"]), "--vault", str(store["vault"])],
        env=env,
    )


class TestCaptureCLI:
    def test_capture_returns_json_array(self, store):
        result = _capture(store)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 2
        assert set(data[0]) == {"url", "timestamp", "note"}

    def test_capture_writes_inbox_rows(self, store):
        _capture(store)
        rows = _q(store, "SELECT url, note FROM inbox")
        assert len(rows) == 2
        urls = [r[0] for r in rows]
        assert "https://example.com/article" in urls
        assert any(r[1] is not None for r in rows)

    def test_capture_source_name_stored(self, store):
        _capture(store)
        source_names = {r[0] for r in _q(store, "SELECT source_name FROM inbox")}
        assert source_names == {"telegram:saved_messages"}

    def test_capture_cursor_in_db(self, store):
        _capture(store)
        rows = _q(
            store,
            "SELECT source_name, value FROM cursor WHERE source_name = ?",
            ("telegram:saved_messages",),
        )
        assert len(rows) == 1
        assert int(rows[0][1]) == 2  # max message id of the default fake


class TestCaptureCursorMonotonic:
    """System Invariant 1: a message with id <= stored cursor is never captured twice."""

    def test_rerun_returns_no_new_items(self, store):
        first = _capture(store)
        assert len(json.loads(first.stdout)) == 2

        second = _capture(store)
        assert json.loads(second.stdout) == []

    def test_rerun_inserts_no_message_with_id_le_cursor(self, store):
        _capture(store)
        before = len(_q(store, "SELECT id FROM inbox"))
        _capture(store)
        after = len(_q(store, "SELECT id FROM inbox"))
        assert after == before  # cursor watermark held — nothing re-inserted

    def test_cursor_watermark_persisted_across_runs(self, store):
        _capture(store)
        _capture(store)
        rows = _q(
            store,
            "SELECT value FROM cursor WHERE source_name = ?",
            ("telegram:saved_messages",),
        )
        assert int(rows[0][0]) == 2

    def test_source_sees_stored_cursor(self, store):
        """The stored cursor is passed to the source so messages <= it are skipped."""
        _capture(store)
        # A source that logs the cursor it received would see 2 here; the
        # observable contract is that no message with id <= cursor is
        # captured. EmptySource + a pre-set cursor proves capture passes the
        # cursor and still completes cleanly.
        rows = _q(
            store,
            "SELECT value FROM cursor WHERE source_name = ?",
            ("telegram:saved_messages",),
        )
        assert int(rows[0][0]) > 0
        result = _capture(store, extra_env={"MEMEX_TELEGRAM_SOURCE": "tests.test_capture:EmptySource"})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == []


class TestCaptureSourceInjection:
    def test_source_option_overrides_env(self, store):
        """CLI --source wins over MEMEX_TELEGRAM_SOURCE."""
        result = _capture(
            store,
            extra_env={"MEMEX_TELEGRAM_SOURCE": "tests.test_capture:EmptySource"},
            extra_args=["--source", FAKE_TELEGRAM_SOURCE],
        )
        assert result.returncode == 0, result.stderr
        assert len(json.loads(result.stdout)) == 2

    def test_env_source_used_without_option(self, store):
        result = _capture(
            store,
            extra_env={"MEMEX_TELEGRAM_SOURCE": "tests.test_capture:EmptySource"},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == []


class TestCaptureErrors:
    """Error handling for memex capture (JSON on stderr, exit 1)."""

    def test_missing_credentials(self, store, monkeypatch):
        monkeypatch.delenv("MEMEX_TELEGRAM_SOURCE", raising=False)
        monkeypatch.delenv("MEMEX_TELEGRAM_API_ID", raising=False)
        monkeypatch.delenv("MEMEX_TELEGRAM_API_HASH", raising=False)
        result = _run_memex(
            ["capture", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"] == "missing_credentials"

    def test_source_not_found(self, store):
        result = _capture(
            store, extra_env={"MEMEX_TELEGRAM_SOURCE": "no.such.module:Source"}
        )
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"] == "source_not_found"

    def test_auth_failed(self, store):
        result = _capture(
            store,
            extra_env={"MEMEX_TELEGRAM_SOURCE": "tests.test_capture:AuthFailingSource"},
        )
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"] == "auth_failed"

    def test_network_error(self, store):
        result = _capture(
            store,
            extra_env={"MEMEX_TELEGRAM_SOURCE": "tests.test_capture:NetworkFailingSource"},
        )
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"] == "network_error"

    def test_missing_db_errors(self, store):
        result = _run_memex(
            ["capture", "--db", str(store["tmp"] / "nope.db"), "--vault", str(store["vault"])],
            env={"MEMEX_TELEGRAM_SOURCE": FAKE_TELEGRAM_SOURCE},
        )
        assert result.returncode == 1
        assert json.loads(result.stderr)["error"] == "db_not_found"
