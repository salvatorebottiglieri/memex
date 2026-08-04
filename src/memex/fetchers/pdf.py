"""PDFFetcher — PDF text extraction via pypdf (imported lazily).

pypdf is an optional dependency (``pip install memex[media]``); when it is
missing the fetcher raises a clear ``FetchError`` instead of crashing.
"""
from __future__ import annotations

import io
from pathlib import Path

from memex.fetchers import FetchError, FetchResult, Fetcher
from memex.fetchers.http import download_bytes


class PDFFetcher(Fetcher):
    """Download a PDF and join per-page extracted text."""

    TYPE = "pdf"

    def fetch(self, url: str, *, cache_dir: Path | None = None) -> FetchResult:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise FetchError(
                "PDF extraction requires pypdf — install it with "
                "`pip install memex[media]` (or `uv add pypdf`)"
            ) from exc

        raw = download_bytes(url)
        try:
            reader = PdfReader(io.BytesIO(raw))
            pages = [page.extract_text() or "" for page in reader.pages]
        except Exception as exc:
            raise FetchError(f"PDF parse failed for {url}: {exc}") from exc

        content = "\n\n".join(pages).strip()
        if not content:
            raise FetchError(f"PDF contained no extractable text: {url}")
        return FetchResult(content=content)
