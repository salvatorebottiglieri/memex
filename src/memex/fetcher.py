"""Content fetcher: HttpFetcher + load_fetcher for test injection."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class FetchError(Exception):
    """Raised when content cannot be fetched."""


@dataclass
class FetchResult:
    """The result of a successful fetch."""

    content: str  # Markdown-compatible article text
    title: str | None = None
    content_path: str | None = None


class ContentFetcher:
    """Protocol / base class for content fetchers.

    Implementations must provide a fetch(url) -> FetchResult method.
    """

    def fetch(self, url: str) -> FetchResult:
        raise NotImplementedError


class HttpFetcher(ContentFetcher):
    """Real HTTP fetcher — extracts article text from HTML."""

    def fetch(self, url: str) -> FetchResult:
        try:
            import urllib.request

            with urllib.request.urlopen(url, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise FetchError(str(exc)) from exc

        title = _extract_title(html)
        content = _html_to_markdown(html, title=title)
        return FetchResult(content=content, title=title)


class PDFFetcher(ContentFetcher):
    """Fetcher that downloads a PDF and extracts text via pypdf."""

    def fetch(self, url: str) -> FetchResult:
        try:
            import urllib.request

            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
        except Exception as exc:
            raise FetchError(str(exc)) from exc

        try:
            import io
            from pypdf import PdfReader

            text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)
        except ImportError as exc:
            raise FetchError("pypdf is required for PDF extraction") from exc
        except Exception as exc:
            raise FetchError(str(exc)) from exc

        return FetchResult(content=text)


class YouTubeTranscriptFetcher(ContentFetcher):
    """Fetcher that extracts YouTube video metadata and transcript.

    Requires ``youtube-transcript-api`` (optional dep, ``pip install memex[media]``).
    When ``vault_path`` is set, writes the transcript to ``{vault_path}/.cache/youtube-{id}.md``.
    """

    def __init__(self, vault_path: str | None = None):
        self._vault_path = vault_path

    def fetch(self, url: str) -> FetchResult:
        from memex.canonical_key import canonical_key

        ckey = canonical_key(url)
        video_id = ckey.removeprefix("youtube://")

        # --- Extract metadata from YouTube page HTML ---
        title = f"YouTube Video {video_id}"
        channel = None
        try:
            import re
            import urllib.request

            with urllib.request.urlopen(url, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            extracted = _extract_title(html)
            if extracted:
                title = extracted
            m = re.search(r'"channelName"\s*:\s*"([^"]+)"', html)
            if m:
                channel = m.group(1)
        except Exception:
            pass

        metadata = f"# {title}"
        if channel:
            metadata += f"\nChannel: {channel}"

        # --- Try to fetch transcript ---
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            transcript = YouTubeTranscriptApi().fetch(video_id)
            transcript_text = "\n".join(seg.text for seg in transcript)

            if self._vault_path:
                cache_dir = Path(self._vault_path) / ".cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path = cache_dir / f"youtube-{video_id}.md"
                cache_path.write_text(transcript_text, encoding="utf-8")
                return FetchResult(content=metadata, title=title, content_path=str(cache_path))
            else:
                return FetchResult(content=metadata + "\n\n" + transcript_text, title=title)
        except ImportError as exc:
            raise FetchError("youtube-transcript-api is required for YouTube transcript extraction") from exc
        except Exception as exc:
            # Check if it's a "transcript unavailable" type error — metadata only
            exc_name = type(exc).__name__
            if exc_name in ("TranscriptsDisabled", "NoTranscriptFound"):
                return FetchResult(content=metadata, title=title)
            # Everything else (network, rate limit, etc.) is a FetchError
            raise FetchError(str(exc)) from exc


class RoutingFetcher(ContentFetcher):
    """Fetcher that dispatches to specific fetchers based on canonical key prefix.

    ``_select(ckey)`` maps the canonical key to the appropriate fetcher:

    - ``youtube://`` -> ``YouTubeTranscriptFetcher``
    - ``http[s]://*.pdf`` -> ``PDFFetcher``
    - everything else -> ``HttpFetcher``
    """

    def __init__(self, fetchers: list[type[ContentFetcher]], vault_path: str | None = None):
        self._fetchers = fetchers
        self._vault_path = vault_path

    def _select(self, ckey: str) -> ContentFetcher:
        if ckey.startswith("youtube://"):
            return YouTubeTranscriptFetcher(vault_path=self._vault_path)
        stripped = ckey.split("?")[0].rstrip("/")
        if ckey.startswith(("http://", "https://")):
            if stripped.endswith(".pdf") or "/pdf/" in stripped:
                return PDFFetcher()
            return HttpFetcher()
        return HttpFetcher()

    def fetch(self, url: str) -> FetchResult:
        from memex.canonical_key import canonical_key
        ckey = canonical_key(url)
        fetcher = self._select(ckey)
        return fetcher.fetch(url)


def _extract_title(html: str) -> str | None:
    import re

    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def _html_to_markdown(html: str, title: str | None = None) -> str:
    """Very simple HTML-to-markdown conversion — good enough for L0 raw source."""
    import re

    # Strip scripts and styles
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    if title:
        return f"# {title}\n\n{text}"
    return text


def load_fetcher(module_path: str | None = None, vault_path: str | None = None):
    """Load a fetcher from a 'module:Class' string, or return the default RoutingFetcher.

    Args:
        module_path: 'module:Class' string, or None for default.
        vault_path: Optional vault path passed through to RoutingFetcher.
    """
    if not module_path:
        return RoutingFetcher([HttpFetcher, PDFFetcher], vault_path=vault_path)
    from memex.plugin import load_class

    return load_class(module_path)
