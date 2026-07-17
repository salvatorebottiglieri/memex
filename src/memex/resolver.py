"""Resolver abstraction for non-ingestable URLs.

When memex encounters a URL it cannot ingest directly (X/Twitter, media),
it can spawn an external agent (Pi, Claude) with browser access to
extract the real target URL and ingest that instead.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod


class ResolverError(Exception):
    """Raised when URL resolution via agent fails."""


class Resolver(ABC):
    """Abstract base for agent-based URL resolvers."""

    @abstractmethod
    def resolve(self, url: str) -> str:
        """Open *url* in a browser and return the resolved target URL."""

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Return True if this resolver's CLI is installed."""


class PiResolver(Resolver):
    """Resolver that uses the Pi agent CLI (non-interactive mode)."""

    PROMPT = (
        "You have a browser available. "
        "Open this URL in the browser: {url} . "
        "Find the actual article/content link it redirects to or contains. "
        "Return ONLY the resolved URL as plain text. Nothing else."
    )

    @classmethod
    def available(cls) -> bool:
        return shutil.which("pi") is not None

    def resolve(self, url: str) -> str:
        prompt = self.PROMPT.format(url=url)
        try:
            result = subprocess.run(
                ["pi", "-p", prompt],
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout.strip()
            if output and (output.startswith("http://") or output.startswith("https://")):
                return output.strip().rstrip(".").rstrip(",")
            raise ResolverError(f"Pi did not return a valid URL: {output[:200]}")
        except subprocess.TimeoutExpired:
            raise ResolverError("Pi timed out resolving URL")
        except FileNotFoundError:
            raise ResolverError("Pi CLI not found")


class _CustomResolver(Resolver):
    """Resolver from MEMEX_RESOLVER_CMD env var."""

    def __init__(self, cmd: str):
        self._cmd = cmd

    @classmethod
    def available(cls) -> bool:
        return True

    def resolve(self, url: str) -> str:
        try:
            result = subprocess.run(
                self._cmd.split() + [url],
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout.strip()
            if output and (output.startswith("http://") or output.startswith("https://")):
                return output.strip().rstrip(".").rstrip(",")
            raise ResolverError(f"Custom resolver did not return a valid URL: {output[:200]}")
        except subprocess.TimeoutExpired:
            raise ResolverError("Custom resolver timed out")
        except FileNotFoundError:
            raise ResolverError("Custom resolver not found")


# Registry: first available wins
_RESOLVERS: list[type[Resolver]] = [PiResolver]


def detect_resolver() -> Resolver | None:
    """Return the first available resolver, or None."""
    custom_cmd = os.environ.get("MEMEX_RESOLVER_CMD")
    if custom_cmd:
        return _CustomResolver(custom_cmd)
    for cls in _RESOLVERS:
        if cls.available():
            return cls()
    return None
