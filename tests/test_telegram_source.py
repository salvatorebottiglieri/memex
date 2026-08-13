"""Tests for the TelegramSource protocol, URL splitting, and the source loader.

Tests use a fake Telegram source injected via MEMEX_TELEGRAM_SOURCE env var.
No real Telegram credentials needed.
"""
from __future__ import annotations

import inspect
import sys
import types

import pytest

from memex.telegram_source import (
    AuthFailedError,
    CapturedMessage,
    CredentialsError,
    NetworkError,
    RealTelegramSource,
    _split_urls_and_note,
    load_telegram_source,
)
from tests.fake_telegram_source import FakeTelegramSource

FAKE_TELEGRAM_SOURCE = "tests.fake_telegram_source:FakeTelegramSource"


class TestSplitUrlsAndNote:
    """Unit tests for _split_urls_and_note."""

    def test_finds_urls_and_strips_them_from_note(self):
        text = "Check this out https://example.com/article and https://x.com/foo"
        urls, note = _split_urls_and_note(text)
        assert len(urls) == 2
        assert urls[0] == "https://example.com/article"
        assert "Check this out" in note
        assert "example.com" not in note

    def test_no_urls_returns_empty_list_and_original_text(self):
        urls, note = _split_urls_and_note("Just some text without links")
        assert urls == []
        assert note == "Just some text without links"

    def test_only_url_returns_empty_note(self):
        urls, note = _split_urls_and_note("https://example.com/article")
        assert len(urls) == 1
        assert note == ""

    def test_handles_http_urls_too(self):
        urls, note = _split_urls_and_note("http://plain.example/one")
        assert urls == ["http://plain.example/one"]
        assert note == ""

    @pytest.mark.parametrize("punctuation", [".", ",", ";", ":", "!", "?"])
    def test_strips_safe_trailing_punctuation(self, punctuation):
        text = f"see https://x.com/a{punctuation}"
        urls, note = _split_urls_and_note(text)
        assert urls == ["https://x.com/a"]
        assert note == "see"

    def test_does_not_strip_closing_paren(self):
        text = "see https://en.wikipedia.org/wiki/Function_(mathematics)"
        urls, note = _split_urls_and_note(text)
        assert urls == ["https://en.wikipedia.org/wiki/Function_(mathematics)"]
        assert note == "see"


class TestLoadTelegramSource:
    def test_loads_module_path(self):
        source = load_telegram_source(FAKE_TELEGRAM_SOURCE)
        assert isinstance(source, FakeTelegramSource)
        assert len(source.capture()) > 0

    def test_raises_credentials_error_without_source_or_creds(self, monkeypatch):
        monkeypatch.delenv("MEMEX_TELEGRAM_API_ID", raising=False)
        monkeypatch.delenv("MEMEX_TELEGRAM_API_HASH", raising=False)
        with pytest.raises(CredentialsError):
            load_telegram_source(None)

    def test_returns_real_source_with_creds(self, monkeypatch):
        monkeypatch.setenv("MEMEX_TELEGRAM_API_ID", "12345")
        monkeypatch.setenv("MEMEX_TELEGRAM_API_HASH", "fakehash")
        monkeypatch.delenv("MEMEX_TELEGRAM_SESSION", raising=False)
        source = load_telegram_source(None)
        assert isinstance(source, RealTelegramSource)
        assert source.api_id == 12345
        assert source.api_hash == "fakehash"


class TestFakeTelegramSource:
    def test_returns_messages(self):
        source = FakeTelegramSource()
        messages = source.capture()
        assert len(messages) >= 1
        assert isinstance(messages[0], CapturedMessage)
        assert messages[0].url is not None
        assert messages[0].timestamp is not None

    def test_accepts_custom_messages(self):
        custom = [
            CapturedMessage(
                url="https://custom.example/1",
                timestamp="2024-06-01T10:00:00",
                note="custom note",
            ),
        ]
        source = FakeTelegramSource(messages=custom)
        assert source.capture() == custom

    def test_filters_by_cursor(self):
        source = FakeTelegramSource(messages=[
            CapturedMessage(url="https://a.example/1", timestamp="2024-06-01T10:00:00", id=5),
            CapturedMessage(url="https://b.example/2", timestamp="2024-06-01T10:00:00", id=9),
        ])
        assert [m.id for m in source.capture(cursor=5)] == [9]
        assert source.capture(cursor=9) == []


class TestRealTelegramSourceReadOnly:
    """System Invariant 3: capture never mutates Telegram.

    The real source must only read (get_messages); write APIs
    (send_message, reactions, ...) must not appear in its code.
    """

    def test_uses_only_read_apis(self):
        src = inspect.getsource(RealTelegramSource)
        # The read path exists (non-vacuous: the class actually fetches).
        assert "get_messages" in src
        for forbidden in (
            "send_message",
            "send_file",
            "edit_message",
            "delete_messages",
            "forward_messages",
            "pin_message",
            "send_reaction",
        ):
            assert forbidden not in src

    def test_error_hierarchy_maps_capture_failures(self):
        assert issubclass(CredentialsError, Exception)
        assert issubclass(AuthFailedError, Exception)
        assert issubclass(NetworkError, Exception)


class TestRealTelegramSourceCaptureCursor:
    """Pins RealTelegramSource.capture cursor semantics without real Telegram.

    Telethon's ``offset_id`` means "older than" (exclusive); passing the
    cursor there would re-fetch the already-captured window, duplicate
    messages in the inbox, and regress the cursor. The source must use
    ``min_id`` (a lower bound) instead.
    """

    @staticmethod
    def _stub_telethon(monkeypatch, calls):
        """Inject a fake telethon module recording get_messages calls."""

        class FakeTelegramClient:
            def __init__(self, *args, **kwargs):
                pass

            async def start(self):
                pass

            async def get_messages(self, *args, **kwargs):
                calls.append((args, kwargs))
                return []

            async def disconnect(self):
                pass

        class FakeAuthError(Exception):
            pass

        class FakeRPCError(Exception):
            pass

        telethon = types.ModuleType("telethon")
        telethon_errors = types.ModuleType("telethon.errors")
        telethon_errors.AuthError = FakeAuthError
        telethon_errors.RPCError = FakeRPCError
        telethon.TelegramClient = FakeTelegramClient
        telethon.errors = telethon_errors
        monkeypatch.setitem(sys.modules, "telethon", telethon)
        monkeypatch.setitem(sys.modules, "telethon.errors", telethon_errors)

    def test_capture_uses_min_id_not_offset_id(self, monkeypatch, tmp_path):
        calls = []
        self._stub_telethon(monkeypatch, calls)
        source = RealTelegramSource(
            api_id=123,
            api_hash="hash",
            session_path=str(tmp_path / "telegram.session"),
        )

        source.capture(cursor=42)
        args, kwargs = calls[0]
        assert args == ("me",)
        assert kwargs == {"limit": 100, "min_id": 42}
        assert "offset_id" not in kwargs

        source.capture(cursor=None)
        args, kwargs = calls[1]
        assert args == ("me",)
        assert kwargs == {"limit": 100, "min_id": 0}
        assert "offset_id" not in kwargs
