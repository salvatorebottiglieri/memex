"""Fake ContentFetcher for tests — no real network calls.

Returns predictable content keyed by URL. URLs matching "fail.example.com"
simulate fetch failures (raise FetchError).
"""
from __future__ import annotations

from memex.fetcher import FetchResult, FetchError


class FakeFetcher:
    """Deterministic fetcher for tests."""

    def fetch(self, url: str) -> FetchResult:
        if "fail.example.com" in url:
            raise FetchError(f"Simulated fetch failure for {url}")
        # Return stable fake content
        return FetchResult(
            content=f"# Fake Article\n\nFake content for {url}",
            title="Fake Article Title",
        )
