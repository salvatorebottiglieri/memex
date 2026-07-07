"""Fake YouTube fetcher for smoke tests — returns metadata + content_path."""

import tempfile
from pathlib import Path

from memex.fetcher import FetchResult


class FakeYouTubeFetcher:
    """Deterministic fetcher that mimics YouTube transcript extraction."""

    CACHE_DIR: Path | None = None

    def fetch(self, url: str) -> FetchResult:
        if self.CACHE_DIR is None:
            self.CACHE_DIR = Path(tempfile.mkdtemp(suffix="_ytcache"))

        # Write transcript to a real cache file
        cache_path = self.CACHE_DIR / "youtube-fakevideo.md"
        cache_path.write_text(
            "Welcome to my test video\n\n"
            "This is a transcript segment.\n"
            "Another segment of the video transcript.\n",
            encoding="utf-8",
        )

        return FetchResult(
            content="# Test YouTube Video\nChannel: Test Channel",
            title="Test YouTube Video",
            content_path=str(cache_path),
        )
