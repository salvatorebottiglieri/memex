"""Content fetcher: HttpFetcher + load_fetcher for test injection."""
from __future__ import annotations

from dataclasses import dataclass


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


class RoutingFetcher(ContentFetcher):
    """Fetcher that dispatches to specific fetchers based on canonical key prefix.

    ``_select(ckey)`` maps the canonical key to the appropriate fetcher:

    - ``youtube://`` → raises ``FetchError`` (not yet implemented)
    - ``http[s]://*.pdf`` → ``PDFFetcher``
    - everything else → ``HttpFetcher``
    """

    def __init__(self, fetchers: list[type[ContentFetcher]]):
        self._fetchers = fetchers

    def _select(self, ckey: str) -> ContentFetcher:
        if ckey.startswith("youtube://"):
            raise FetchError("YouTube fetcher not yet implemented")
        stripped = ckey.split("?")[0].rstrip("/")
        if ckey.startswith(("http://", "https://")) and stripped.endswith(".pdf"):
            return PDFFetcher()
        return HttpFetcher()

    def fetch(self, url: str) -> FetchResult:
        ckey = url  # canonical_key is applied by the caller (ingester)
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


def load_fetcher(module_path: str | None = None):
    """Load a fetcher from a 'module:Class' string, or return the default RoutingFetcher."""
    if not module_path:
        return RoutingFetcher([HttpFetcher, PDFFetcher])
    from memex.plugin import load_class
    return load_class(module_path)
