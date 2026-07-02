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


class TelegramSourceError(Exception):
    """Base exception for Telegram source errors."""


class CredentialsError(TelegramSourceError):
    """Missing or invalid credentials."""


class AuthFailedError(TelegramSourceError):
    """Telegram authentication failed (expired session, wrong credentials)."""


class NetworkError(TelegramSourceError):
    """Network or Telegram API error."""


class TelegramSource(Protocol):
    """Protocol for Telegram message sources.

    Implementations must provide a capture() -> list[CapturedMessage] method.
    """

    def capture(self) -> list[CapturedMessage]:
        ...


def _extract_urls(text: str) -> list[str]:
    """Extract all URLs from a text string."""
    import re
    return re.findall(r"https?://\S+", text)


def _message_note(text: str) -> str:
    """Strip URLs from a message text to produce a note."""
    import re
    return re.sub(r"https?://\S+\s*", "", text).strip()


def load_telegram_source(module_path: str | None = None) -> TelegramSource:
    """Load a Telegram source from a 'module:Class' string.

    If no ``module_path`` is provided, attempts to create a
    ``RealTelegramSource`` from ``MEMEX_TELEGRAM_API_ID`` and
    ``MEMEX_TELEGRAM_API_HASH`` env vars. Falls back to
    ``ImportError`` with instructions.

    Returns:
        An instance of the requested TelegramSource class.

    Raises:
        ImportError: If no source can be loaded (no module_path and
                     missing credentials, or bad module/class path).
    """
    if module_path:
        module_name, _, class_name = module_path.partition(":")
        import importlib
        mod = importlib.import_module(module_name)
        cls = getattr(mod, class_name)
        return cls()

    # No override — try real Telegram source
    import os as _os
    api_id = _os.environ.get("MEMEX_TELEGRAM_API_ID")
    api_hash = _os.environ.get("MEMEX_TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise CredentialsError(
            "Set MEMEX_TELEGRAM_API_ID and MEMEX_TELEGRAM_API_HASH environment "
            "variables, or set MEMEX_TELEGRAM_SOURCE for testing."
        )
    session_path = _os.environ.get("MEMEX_TELEGRAM_SESSION")
    return RealTelegramSource(api_id=int(api_id), api_hash=api_hash,
                               session_path=session_path)


class RealTelegramSource:
    """Real MTProto-backed Telegram source using Telethon.

    Connects to Telegram, reads Saved Messages, and returns new messages
    with URLs as ``CapturedMessage`` items.

    Requires ``MEMEX_TELEGRAM_API_ID`` and ``MEMEX_TELEGRAM_API_HASH``
    environment variables, or pass them directly.
    """

    def __init__(self, api_id: int, api_hash: str, session_path: str | None = None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path or "~/.memex/telegram.session"

    def capture(self) -> list[CapturedMessage]:
        """Fetch messages from Telegram Saved Messages.

        Returns all recent messages containing URLs. Does NOT filter by
        cursor — that is the CLI's responsibility using the cursor table.

        Returns:
            List of ``CapturedMessage`` for each URL found.
        """
        import asyncio
        import os
        from pathlib import Path

        from telethon import TelegramClient
        from telethon.errors import AuthError, RPCError

        session = os.path.expanduser(self.session_path)
        Path(session).parent.mkdir(parents=True, exist_ok=True)

        async def _fetch():
            client = TelegramClient(session, self.api_id, self.api_hash)
            try:
                await client.start()
                msgs = await client.get_messages("me", limit=100)
            except AuthError as e:
                raise AuthFailedError(f"Telegram authentication failed: {e}") from e
            except RPCError as e:
                raise NetworkError(f"Telegram API error: {e}") from e
            except OSError as e:
                raise NetworkError(f"Telegram network error: {e}") from e
            finally:
                await client.disconnect()

            result = []
            for msg in msgs:
                text = msg.text
                if not text:
                    continue
                urls = _extract_urls(text)
                if not urls:
                    continue
                note = _message_note(text)
                ts = msg.date.isoformat() if msg.date else ""
                for url in urls:
                    result.append(CapturedMessage(
                        url=url,
                        timestamp=ts,
                        note=note or None,
                    ))
            return result

        return asyncio.get_event_loop().run_until_complete(_fetch())
