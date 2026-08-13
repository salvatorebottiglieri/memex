"""Fake TelegramSource for tests — no real Telegram calls.

Returns configurable captured messages with incremental IDs to
simulate cursor advancement.
"""
from __future__ import annotations

from memex.telegram_source import CapturedMessage, TelegramSource


class FakeTelegramSource(TelegramSource):
    """Deterministic fake Telegram source for tests."""

    _counter = 0

    def __init__(self, messages: list[CapturedMessage] | None = None):
        if messages is not None:
            self.messages = messages
        else:
            FakeTelegramSource._counter += 2
            self.messages = [
                CapturedMessage(
                    url="https://example.com/article",
                    timestamp="2024-06-01T09:00:00",
                    note="interesting read about testing",
                    id=FakeTelegramSource._counter - 1,
                ),
                CapturedMessage(
                    url="https://news.example.com/story",
                    timestamp="2024-06-01T10:00:00",
                    note="Check this out important news",
                    id=FakeTelegramSource._counter,
                ),
            ]

    def capture(self, cursor: int | None = None) -> list[CapturedMessage]:
        """Return only messages with id > cursor (simulates Telethon offset_id)."""
        if cursor is None:
            return self.messages
        return [m for m in self.messages if m.id is not None and m.id > cursor]
