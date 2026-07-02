"""TelegramSource protocol and source loader for memex capture.

Tests inject FakeTelegramSource via MEMEX_TELEGRAM_SOURCE env var.

Slice 2: protocol + fake only. The real Telethon integration comes in slice 3.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CapturedMessage:
    """A single message captured from a Telegram source."""

    url: str
    timestamp: str  # ISO-8601
    note: str | None = None


class TelegramSource(Protocol):
    """Protocol for Telegram message sources.

    Implementations must provide a capture() -> list[CapturedMessage] method.
    """

    def capture(self) -> list[CapturedMessage]:
        ...


def load_telegram_source(module_path: str | None = None) -> TelegramSource:
    """Load a Telegram source from a 'module:Class' string.

    Args:
        module_path: A string like 'tests.fake_telegram_source:FakeTelegramSource'.
                     Must be provided — no default source in slice 2.

    Returns:
        An instance of the requested TelegramSource class.

    Raises:
        ImportError: If module_path is None or the module/class cannot be loaded.
    """
    if not module_path:
        raise ImportError(
            "MEMEX_TELEGRAM_SOURCE is not set. "
            "Set it to e.g. 'tests.fake_telegram_source:FakeTelegramSource' "
            "for testing, or install a real Telegram source for production use."
        )
    module_name, _, class_name = module_path.partition(":")
    import importlib
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    return cls()
