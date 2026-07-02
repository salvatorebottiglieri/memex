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
    """Load a fetcher from a 'module:Class' string, or return the default HttpFetcher.

    Used by the CLI to allow test injection via MEMEX_FETCHER_MODULE env var.
    """
    if not module_path:
        return HttpFetcher()
    module_name, _, class_name = module_path.partition(":")
    import importlib
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    return cls()
