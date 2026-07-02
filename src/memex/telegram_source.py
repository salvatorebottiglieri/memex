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
    id: int | None = None  # Telegram message ID for cursor tracking


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

    def capture(self, cursor: int | None = None) -> list[CapturedMessage]:
        ...


def _split_urls_and_note(text: str) -> tuple[list[str], str]:
    """Extract URLs and produce a note (text with URLs stripped)."""
    import re
    urls = re.findall(r"https?://\S+", text)
    note = re.sub(r"https?://\S+\s*", "", text).strip()
    return urls, note


def load_telegram_source(module_path: str | None = None) -> TelegramSource:
    """Load a Telegram source from a ``module:Class`` string.

    Falls back to ``RealTelegramSource`` from ``MEMEX_TELEGRAM_API_ID``
    and ``MEMEX_TELEGRAM_API_HASH`` env vars if no override is provided.
    """
    if module_path:
        from memex.plugin import load_class
        return load_class(module_path)

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

    def capture(self, cursor: int | None = None) -> list[CapturedMessage]:
        """Fetch messages from Telegram Saved Messages after the given cursor.

        Args:
            cursor: Last seen Telegram message ID; only newer messages are fetched.

        Returns:
            List of ``CapturedMessage`` with ``id`` set to the Telegram message ID.
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
                msgs = await client.get_messages("me", limit=100, offset_id=cursor or 0)
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
                urls, note = _split_urls_and_note(text)
                if not urls:
                    continue
                ts = msg.date.isoformat() if msg.date else ""
                for url in urls:
                    result.append(CapturedMessage(
                        url=url,
                        timestamp=ts,
                        note=note or None,
                        id=msg.id,
                    ))
            return result

        return asyncio.run(_fetch())
