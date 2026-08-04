"""YouTubeTranscriptFetcher — transcript extraction for YouTube watch URLs.

Implements ADR-0013 for YouTube: ``youtube-transcript-api`` is a lazy,
optional dependency (``pip install memex[media]``); importing this module or
``memex.fetchers`` never requires it. The fetcher caches the transcript at
``$VAULT/.cache/youtube-<video-id>.md`` (immutable once written — delete the
file manually to force a refresh) and returns metadata as ``FetchResult.content``
with ``content_path`` pointing at the cache file.

Error model (defensive, verified against youtube-transcript-api 1.2.4's
``_errors.py`` and what ``fetch()`` raises): ``TranscriptsDisabled`` /
``NoTranscriptFound`` / ``VideoUnavailable`` / ``VideoUnplayable`` /
``AgeRestricted`` are content outcomes — the fetcher returns a metadata-only
result (``content_path=None``, no cache file, no ``FetchError``) so the
extracted node is created anyway and derive fails gracefully. Every other
exception (``RequestBlocked`` / ``IpBlocked`` / ``YouTubeRequestFailed`` /
``YouTubeDataUnparsable``, connection errors, timeouts, …) is an
infrastructure failure and raises ``FetchError``, matching the generic
fetcher contract.
"""
from __future__ import annotations

import json
import os
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlencode

from memex.fetchers import FetchError, FetchResult, Fetcher

_USER_AGENT = "memex/0.1 (+https://github.com/salvatore/memex; extract)"
_OEMBED_URL = "https://www.youtube.com/oembed"
_OEMBED_TIMEOUT = 5.0

# Class names of the youtube-transcript-api 1.2.x content outcomes (verified
# against the real package's ``_errors`` module and what ``fetch()`` raises) —
# matched by name so the classification works whether the real package is
# installed or a test injects a fake client raising its own exceptions.
# ``VideoUnplayable`` (region/embed restrictions) and ``AgeRestricted``
# (login-gated video) are also content outcomes in 1.2.x: YouTube served a
# valid page for an existing video, just no obtainable transcript.
_NO_TRANSCRIPT_ERROR_NAMES = frozenset(
    {
        "TranscriptsDisabled",
        "NoTranscriptFound",
        "VideoUnavailable",
        "VideoUnplayable",
        "AgeRestricted",
    }
)


def _is_no_transcript_error(exc: BaseException) -> bool:
    """True when *exc* is one of the documented no-transcript outcomes.

    Checks the class name of the exception and of its MRO ancestors, so
    subclasses (e.g. a package version that subclasses ``NoTranscriptFound``)
    are recognised too.
    """
    return any(
        cls.__name__ in _NO_TRANSCRIPT_ERROR_NAMES for cls in type(exc).__mro__
    )


def _watch_url(video_id: str) -> str:
    """Canonical watch URL for a video id (the form oEmbed understands)."""
    return f"https://www.youtube.com/watch?v={video_id}"


class YouTubeTranscriptFetcher(Fetcher):
    """Fetch a video's transcript and cache it under ``cache_dir``.

    ``client`` is injectable for tests: any object exposing
    ``fetch(video_id)`` returning an object with a ``to_raw_data()`` method
    (the ``FetchedTranscript`` surface of youtube-transcript-api >= 1.2)
    yielding a list of segments with ``text`` keys. Defaults to
    ``youtube_transcript_api.YouTubeTranscriptApi()``, imported lazily so the
    module imports without the dependency installed.
    """

    TYPE = "youtube"

    def __init__(self, client=None):
        self._client = client

    # ------------------------------------------------------------------
    # Fetcher contract
    # ------------------------------------------------------------------

    def fetch(self, url: str, *, cache_dir: Path | None = None) -> FetchResult:
        """Fetch the transcript for a YouTube watch URL.

        Outcomes:

        - cache file present -> return it without touching the API
          (immutable cache, ADR-0013).
        - transcript available -> write the cache file, return metadata +
          ``content_path``.
        - transcript disabled/unavailable -> return metadata only,
          ``content_path=None``, no cache file, no ``FetchError``.
        - anything else (network, rate-limit, …) -> ``FetchError``.
        """
        video_id = self._video_id(url)
        if video_id is None:
            raise FetchError(f"not a youtube watch url: {url}")

        cache: Path | None = None
        if cache_dir is not None:
            cache = Path(cache_dir) / f"youtube-{video_id}.md"

        # Cache-first (immutable): never hit the API (or the network) again.
        if cache is not None and cache.exists():
            return FetchResult(
                content=self._metadata_md(
                    video_id, available=True, cached=True
                ),
                content_path=str(cache),
                title=self._title_from_cache(cache),
            )

        try:
            # youtube-transcript-api >= 1.2: fetch() returns a
            # FetchedTranscript; to_raw_data() yields segment dicts with
            # 'text' keys (the shape _cache_content consumes). Filter to
            # segments with non-empty text up front so the cache-header
            # counts (metadata + Synthesis line) agree with the written body.
            segments = [
                s
                for s in self._get_client().fetch(video_id).to_raw_data()
                if s.get("text")
            ]
        except Exception as exc:
            if _is_no_transcript_error(exc):
                # Content outcome: metadata-only node, graceful derive
                # failure later. No cache file, no FetchError.
                title, channel = self._oembed_meta(video_id)
                return FetchResult(
                    content=self._metadata_md(video_id, available=False, channel=channel),
                    content_path=None,
                    title=title,
                )
            raise FetchError(
                f"youtube transcript fetch failed for {url}: {exc}"
            ) from exc

        title, channel = self._oembed_meta(video_id)
        if cache is not None:
            # Disk I/O is infrastructure (ADR-0013): surface cache-write
            # failures as FetchError, not a raw traceback.
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                # Write to a sibling temp file and atomically rename onto the
                # final path so a crash mid-write can never leave a torn cache
                # file that the cache-first branch would treat as immutable.
                tmp = cache.with_name(f"{cache.name}.{uuid.uuid4().hex}.tmp")
                try:
                    tmp.write_text(
                        self._cache_content(video_id, segments, title=title, channel=channel),
                        encoding="utf-8",
                    )
                    os.replace(tmp, cache)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
            except OSError as exc:
                raise FetchError(
                    f"youtube transcript cache write failed for {url}: {exc}"
                ) from exc
        return FetchResult(
            content=self._metadata_md(
                video_id, available=True, segments=len(segments), channel=channel
            ),
            content_path=str(cache) if cache is not None else None,
            title=title,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _video_id(self, url: str) -> str | None:
        """Extract the video id from a YouTube watch URL (canonical helper)."""
        from urllib.parse import urlparse

        from memex.canonical_key import _youtube_id

        return _youtube_id(urlparse(url))

    def _get_client(self):
        """Return the transcript client (lazy import of the optional dep)."""
        if self._client is not None:
            return self._client
        from youtube_transcript_api import YouTubeTranscriptApi

        return YouTubeTranscriptApi()

    def _oembed_meta(self, video_id: str) -> tuple[str | None, str | None]:
        """Best-effort (title, channel) via the YouTube oEmbed endpoint.

        Never raises: any failure (network, non-200, bad JSON) yields
        ``(None, None)``.
        """
        params = urlencode(
            {"url": _watch_url(video_id), "format": "json"}
        )
        request = urllib.request.Request(
            f"{_OEMBED_URL}?{params}", headers={"User-Agent": _USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=_OEMBED_TIMEOUT) as resp:
                if getattr(resp, "status", 200) != 200:
                    return None, None
                data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict):
                    return None, None
                return data.get("title"), data.get("author_name")
        except Exception:
            return None, None

    def _metadata_md(
        self,
        video_id: str,
        *,
        available: bool,
        cached: bool = False,
        segments: int | None = None,
        channel: str | None = None,
    ) -> str:
        """Small markdown metadata — the extracted-node content when the
        transcript is unavailable (or the result content on cache hits)."""
        lines = [
            f"video_id: {video_id}",
            f"canonical_url: {_watch_url(video_id)}",
        ]
        if channel:
            lines.append(f"channel: {channel}")
        if available:
            lines.append("transcript_available: true")
            if segments is not None:
                lines.append(f"segments: {segments}")
        else:
            lines.append("transcript_available: false")
        if cached:
            lines.append("cached: true")
        return "\n".join(lines) + "\n"

    def _cache_content(
        self,
        video_id: str,
        segments: list[dict],
        *,
        title: str | None,
        channel: str | None,
    ) -> str:
        """Immutable cache artifact: metadata header + transcript body.

        The body is one segment text per line. The ``> Synthesis:`` line
        states the artifact's identity — the shared checks-to-trust gate
        (D3) requires a synthesis marker in the content file for the
        extracted node to reach ``auto-verified``, mirroring how the web/PDF
        extract tests embed a marker in fetched content.
        """
        lines: list[str] = []
        if title:
            lines.append(f"# {title}")
        lines.append(f"video_id: {video_id}")
        lines.append(f"canonical_url: {_watch_url(video_id)}")
        if channel:
            lines.append(f"channel: {channel}")
        lines.append("transcript_available: true")
        lines.append(f"segments: {len(segments)}")
        lines.append("")
        who = title or f"video {video_id}"
        by = f" by {channel}" if channel else ""
        lines.append(f"> Synthesis: Transcript of {who}{by} ({len(segments)} segments).")
        lines.append("")
        lines.extend(
            seg.get("text", "").strip() for seg in segments if seg.get("text")
        )
        return "\n".join(lines) + "\n"

    def _title_from_cache(self, cache: Path) -> str | None:
        """Read the title back from the cache header (no network)."""
        try:
            first = cache.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            return None
        if first.startswith("# "):
            return first[2:].strip()
        return None
