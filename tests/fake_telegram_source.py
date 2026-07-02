"""Fake TelegramSource for tests — no real Telegram calls.

Returns configurable captured messages. Used by test_telegram_source.py.
"""
from __future__ import annotations

from memex.telegram_source import CapturedMessage, TelegramSource


class FakeTelegramSource(TelegramSource):
    """Deterministic fake Telegram source for tests."""

    def __init__(self, messages: list[CapturedMessage] | None = None):
        self.messages = messages or [
            CapturedMessage(
                url="https://example.com/article",
                timestamp="2024-06-01T09:00:00",
                note="interesting read about testing",
            ),
            CapturedMessage(
                url="https://news.example.com/story",
                timestamp="2024-06-01T10:00:00",
                note="Check this out important news",
            ),
        ]

    def capture(self) -> list[CapturedMessage]:
        """Return the configured messages."""
        return self.messages
