"""Fetchers — per-type URL fetching behind a small router.

The router picks a fetcher class from the resolution type, with a
direct-'.pdf'-URL override, so per-type extraction is extensible: a new
type (e.g. ``youtube`` for ticket #99) only needs a key in ``_FETCHERS``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from memex.resolve.rules import Resolution


@dataclass
class FetchResult:
    """Text content fetched from a URL, plus an optional page title.

    ``content_path`` (ADR-0013) points at an artifact the fetcher wrote
    itself (e.g. a cached YouTube transcript in ``$VAULT/.cache/``) and is
    opt-in: HttpFetcher/PDFFetcher never set it. When set, the CLI uses that
    file as the extracted node's content instead of writing ``content``.
    """

    content: str
    content_path: str | None = None
    title: str | None = None


class FetchError(Exception):
    """A fetch failed: non-200 status, network error, or parse failure."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Fetcher:
    """Base class for per-type fetchers."""

    TYPE: str = ""

    def fetch(self, url: str, *, cache_dir: Path | None = None) -> FetchResult:
        """Fetch ``url`` and return a ``FetchResult``.

        ``cache_dir`` lets fetchers that produce an external artifact (e.g.
        the YouTube transcript cache, ADR-0013) place it in the vault's
        cache directory. Fetching fetchers ignore it.
        """
        raise NotImplementedError


# Shared types above must be defined before the submodule imports below
# (http.py and pdf.py import FetchError/FetchResult from this module).
from memex.fetchers.http import HttpFetcher  # noqa: E402
from memex.fetchers.pdf import PDFFetcher  # noqa: E402
from memex.fetchers.wikipedia import WikipediaFetcher  # noqa: E402
from memex.fetchers.youtube import YouTubeTranscriptFetcher  # noqa: E402

# Extensible registry: resolution.type -> fetcher class. Per-type fetchers
# (e.g. youtube for ticket #99) add a key here.
_FETCHERS: dict[str, type[Fetcher]] = {
    "arxiv": PDFFetcher,
    "wikipedia": WikipediaFetcher,
    "youtube": YouTubeTranscriptFetcher,
}


def _is_pdf_url(url: str) -> bool:
    """True when the URL's *path* (not its query string or fragment) ends in ``.pdf``."""
    return urlsplit(url).path.lower().endswith(".pdf")


def get_fetcher(url: str, resolution: Resolution) -> type[Fetcher]:
    """Return the fetcher class for a URL + its resolution.

    Any URL whose fetch target's path ends in ``.pdf`` (an arxiv
    ``direct_url`` or a plain ``https://…/paper.pdf`` — query strings and
    fragments included) routes to the PDF fetcher — except wikipedia
    resolutions, whose ``direct_url`` is the REST summary endpoint: a wiki
    title ending in ``.pdf`` (e.g. ``File:Example.pdf``) must stay on the
    Wikipedia fetcher. Every other resolution type maps through the
    registry, defaulting to the HTTP fetcher.
    """
    target = resolution.direct_url or url
    if resolution.type != "wikipedia" and _is_pdf_url(target):
        return PDFFetcher
    return _FETCHERS.get(resolution.type, HttpFetcher)


def fetch(url: str, resolution: Resolution, *, cache_dir: Path | None = None) -> FetchResult:
    """Fetch ``url`` through the fetcher selected by ``resolution``.

    ``cache_dir`` is forwarded to the selected fetcher — the YouTube
    transcript fetcher caches there (ADR-0013). Raises ``FetchError`` when
    the fetch or extraction fails.
    """
    target = resolution.direct_url or url
    return get_fetcher(url, resolution)().fetch(target, cache_dir=cache_dir)


__all__ = [
    "FetchError",
    "FetchResult",
    "Fetcher",
    "HttpFetcher",
    "PDFFetcher",
    "WikipediaFetcher",
    "YouTubeTranscriptFetcher",
    "fetch",
    "get_fetcher",
]
