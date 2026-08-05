"""HttpFetcher — stdlib-urllib fetch of HTML pages.

No third-party dependencies: user-agent header, timeout, redirects followed
(urlopen default), ``<title>`` extraction, and basic HTML tag stripping.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path

from memex.fetchers import FetchError, FetchResult, Fetcher

_USER_AGENT = "memex/0.1 (+https://github.com/salvatore/memex; extract)"
_TIMEOUT = 30.0

# Hard cap on downloaded bodies: past this the response is aborted instead of
# buffered, so a misbehaving server can never OOM the process.
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# Script/style blocks are executable or presentational payload, never page
# text: a JS bundle or stylesheet must not survive tag stripping as "content"
# (stress campaign — issues #111/#112 + the stored CSS/JS junk in the vault).
_SCRIPT_RE = re.compile(r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<\s*style\b[^>]*>.*?<\s*/\s*style\s*>", re.IGNORECASE | re.DOTALL)


def download_bytes(url: str) -> bytes:
    """Download ``url`` and return raw bytes.

    The body is read in bounded chunks and aborted with a ``FetchError`` as
    soon as it exceeds ``_MAX_DOWNLOAD_BYTES``; an oversized Content-Length
    header is rejected up front without reading anything.

    Raises ``FetchError`` on any non-200 status or network error.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise FetchError(f"HTTP {status}: {url}")
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > _MAX_DOWNLOAD_BYTES:
                        raise FetchError(
                            f"content too large ({content_length} bytes, "
                            f"cap {_MAX_DOWNLOAD_BYTES}): {url}"
                        )
                except ValueError:
                    pass  # malformed header — fall back to the streaming cap
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    raise FetchError(
                        f"content too large (>{_MAX_DOWNLOAD_BYTES} bytes): {url}"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code}: {url}") from exc
    except Exception as exc:
        raise FetchError(f"network error fetching {url}: {exc}") from exc


class HttpFetcher(Fetcher):
    """Fetch an HTML page; return stripped text plus the ``<title>``."""

    TYPE = "http"

    def fetch(self, url: str, *, cache_dir: Path | None = None) -> FetchResult:
        raw = download_bytes(url)
        html = raw.decode("utf-8", errors="replace")

        # Junk never becomes content: drop script/style bodies first.
        html = _SCRIPT_RE.sub(" ", html)
        html = _STYLE_RE.sub(" ", html)

        title: str | None = None
        m = _TITLE_RE.search(html)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip() or None

        text = _TAG_RE.sub(" ", html)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        text = text.strip()
        # Expected content absence (ADR-0013): a page with no extractable
        # text (JS-only, image-only) is not an infrastructure failure — the
        # caller decides what to store. Return the metadata and empty content.
        return FetchResult(content=text, title=title)
