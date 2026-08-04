"""WikipediaFetcher — JSON-aware fetch of the MediaWiki REST summary endpoint.

``WikipediaRule`` resolves ``*.wikipedia.org/wiki/...`` to the REST API
``page/summary`` endpoint, whose body is ``application/json``. The generic
``HttpFetcher`` would HTML-strip that JSON into raw machine text, so this
fetcher parses the summary object and renders title + description + extract
as readable prose.
"""
from __future__ import annotations

import json

from memex.fetchers import FetchError, FetchResult, Fetcher
from memex.fetchers.http import download_bytes


class WikipediaFetcher(Fetcher):
    """Fetch a REST summary JSON object; return title + extract as prose."""

    TYPE = "wikipedia"

    def fetch(self, url: str) -> FetchResult:
        raw = download_bytes(url)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FetchError(f"Wikipedia summary parse failed for {url}: {exc}") from exc

        # Valid JSON may be a list/str/number/null; only an object can carry
        # the summary fields, so refuse anything else with a clean FetchError
        # instead of an uncaught AttributeError on .get.
        if not isinstance(data, dict):
            raise FetchError(f"Wikipedia summary for {url} is not a JSON object")

        title = data.get("title")
        description = data.get("description")
        extract = data.get("extract")

        # Error-shaped responses (e.g. HyperSwitch not_found) carry a title
        # but no prose; refuse them instead of extracting a one-line error.
        if not extract and not description:
            raise FetchError(f"Wikipedia summary for {url} has no readable prose")

        parts: list[str] = []
        if title:
            parts.append(f"# {title}")
        if description:
            parts.append(f"> {description}")
        if extract:
            parts.append(extract)
        content = "\n\n".join(parts).strip()
        return FetchResult(content=content, title=title)
