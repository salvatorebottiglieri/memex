"""Tests for ticket #99 — YouTube transcript extractor.

Covers the ``YouTubeTranscriptFetcher`` unit surface (fake transcript client
injected — youtube-transcript-api is NOT installed in the test venv and must
stay optional), routing/resolution to ``type=youtube``, and the ``memex
extract`` / ``memex derive`` flows on YouTube URLs. All fetches are hermetic:
the transcript client and the oEmbed title lookup are faked.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from memex.cli import cli
from memex.fetchers import FetchError, YouTubeTranscriptFetcher, fetch, get_fetcher
from memex.resolve.rules import Resolution, resolve_url

from tests.conftest import _counts, _run_memex

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"


# ── fakes ──────────────────────────────────────────────────────────

# The fetcher classifies no-transcript vs infrastructure failures by the
# *class name* of the raised exception (verified against the
# youtube-transcript-api 1.2.4 source: TranscriptsDisabled / NoTranscriptFound
# / VideoUnavailable / VideoUnplayable / AgeRestricted are content outcomes;
# RequestBlocked and anything else are infrastructure failures). The fakes
# below reuse the documented names so the same code path is exercised with and
# without the real package installed.
class TranscriptsDisabled(Exception):
    pass


class NoTranscriptFound(Exception):
    pass


class VideoUnavailable(Exception):
    pass


class VideoUnplayable(Exception):
    pass


class AgeRestricted(Exception):
    pass


class RequestBlocked(Exception):
    pass


class TooManyRequests(Exception):
    pass


class _FakeTranscript:
    """Mirror of youtube-transcript-api 1.2.x ``FetchedTranscript``: the
    fetcher consumes ``fetch(video_id).to_raw_data()``."""

    def __init__(self, segments: list[dict]):
        self._segments = segments

    def to_raw_data(self) -> list[dict]:
        return [dict(seg) for seg in self._segments]


class _FakeClient:
    """Fake youtube-transcript-api client — records calls, returns segments
    or raises a configured error (mirrors the real 1.2.x surface:
    ``fetch(video_id)`` returning a ``FetchedTranscript``-like object)."""

    def __init__(self, segments: list[dict] | None = None, error: Exception | None = None):
        self._segments = segments or []
        self._error = error
        self.calls: list[str] = []

    def fetch(self, video_id: str):
        self.calls.append(video_id)
        if self._error is not None:
            raise self._error
        return _FakeTranscript(self._segments)


_SEGMENTS = [
    {"text": "This is the first segment of the fake transcript. "},
    {"text": "It contains enough text to exceed the minimum size threshold. "},
    {"text": "The video discusses machine learning foundations and applications."},
]

_WATCH_URL = "https://www.youtube.com/watch?v=abc123"


def _extract(
    store,
    monkeypatch,
    url: str,
    segments: list[dict] | None = None,
    error: Exception | None = None,
    oembed: tuple[str | None, str | None] | None = ("Fake Video Title", "Fake Channel"),
    force: bool = False,
) -> dict:
    """Run `memex extract <url> [--force]` in-process with a faked client."""
    monkeypatch.setattr(
        YouTubeTranscriptFetcher,
        "_get_client",
        lambda self: _FakeClient(segments=segments, error=error),
    )
    monkeypatch.setattr(
        YouTubeTranscriptFetcher, "_oembed_meta", lambda self, video_id: oembed
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "extract",
            *(["--force"] if force else []),
            "--db",
            str(store["db"]),
            "--vault",
            str(store["vault"]),
            url,
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ── YouTubeTranscriptFetcher unit tests ────────────────────────────

class TestYouTubeTranscriptFetcher:
    def test_transcript_available_writes_cache_file(self, tmp_path, monkeypatch):
        fetcher = YouTubeTranscriptFetcher(client=_FakeClient(segments=_SEGMENTS))
        monkeypatch.setattr(
            YouTubeTranscriptFetcher,
            "_oembed_meta",
            lambda self, video_id: ("Fake Video Title", "Fake Channel"),
        )
        result = fetcher.fetch(_WATCH_URL, cache_dir=tmp_path)

        cache = tmp_path / "youtube-abc123.md"
        assert cache.exists()
        text = cache.read_text(encoding="utf-8")
        for seg in _SEGMENTS:
            assert seg["text"].strip() in text
        # No injected fake synthesis marker (ticket #138): the extracted node
        # auto-verifies without it.
        assert "> Synthesis:" not in text
        assert result.content_path == str(cache)
        assert result.title == "Fake Video Title"
        # content is metadata, not the transcript body
        assert "video_id: abc123" in result.content
        assert "transcript_available: true" in result.content
        assert "segments: 3" in result.content
        assert "This is the first segment" not in result.content

    def test_cache_hit_does_not_call_client(self, tmp_path):
        cache = tmp_path / "youtube-abc123.md"
        cache.write_text("# Cached Video\nvideo_id: abc123\n\ntranscript line\n", encoding="utf-8")
        fake = _FakeClient(segments=[{"text": "must not be fetched"}])
        fetcher = YouTubeTranscriptFetcher(client=fake)
        result = fetcher.fetch(_WATCH_URL, cache_dir=tmp_path)

        assert fake.calls == []
        assert result.content_path == str(cache)
        assert result.title == "Cached Video"  # read back from the cache header
        assert "cached: true" in result.content
        assert cache.read_text(encoding="utf-8") == "# Cached Video\nvideo_id: abc123\n\ntranscript line\n"

    @pytest.mark.parametrize(
        "exc_cls",
        [
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
            VideoUnplayable,
            AgeRestricted,
        ],
    )
    def test_no_transcript_returns_empty_content(self, tmp_path, monkeypatch, exc_cls):
        """A video without a transcript is an expected absence (ADR-0013,
        ticket #140): the fetcher returns empty content and no content_path —
        no metadata stub, no cache file. The extract command then registers
        URL+source and reports no_content."""
        fetcher = YouTubeTranscriptFetcher(client=_FakeClient(error=exc_cls("abc123")))
        monkeypatch.setattr(
            YouTubeTranscriptFetcher, "_oembed_meta", lambda self, video_id: (None, None)
        )
        result = fetcher.fetch(_WATCH_URL, cache_dir=tmp_path)

        assert result.content_path is None
        assert result.title is None
        assert result.content == ""
        assert list(tmp_path.glob("youtube-*.md")) == []

    @pytest.mark.parametrize(
        "exc_cls", [RequestBlocked, TooManyRequests, ConnectionError, RuntimeError]
    )
    def test_infrastructure_error_raises_fetch_error(self, tmp_path, exc_cls):
        fetcher = YouTubeTranscriptFetcher(client=_FakeClient(error=exc_cls("abc123")))
        with pytest.raises(FetchError):
            fetcher.fetch(_WATCH_URL, cache_dir=tmp_path)
        assert list(tmp_path.glob("youtube-*.md")) == []

    def test_oembed_failure_is_non_fatal(self, tmp_path, monkeypatch):
        """The best-effort title lookup must never break the fetch."""
        import urllib.error

        def fail_open(request, timeout=None):
            raise urllib.error.URLError("no network")

        monkeypatch.setattr("memex.fetchers.youtube.urllib.request.urlopen", fail_open)
        fetcher = YouTubeTranscriptFetcher(client=_FakeClient(segments=_SEGMENTS))
        result = fetcher.fetch(_WATCH_URL, cache_dir=tmp_path)

        assert result.title is None
        assert result.content_path is not None
        assert (tmp_path / "youtube-abc123.md").exists()

    def test_without_cache_dir_writes_nothing(self, monkeypatch):
        fetcher = YouTubeTranscriptFetcher(client=_FakeClient(segments=_SEGMENTS))
        monkeypatch.setattr(
            YouTubeTranscriptFetcher, "_oembed_meta", lambda self, video_id: ("T", None)
        )
        result = fetcher.fetch(_WATCH_URL)

        assert result.content_path is None
        assert result.title == "T"

    def test_non_youtube_url_raises_fetch_error(self, tmp_path):
        fetcher = YouTubeTranscriptFetcher(client=_FakeClient())
        with pytest.raises(FetchError, match="not a youtube"):
            fetcher.fetch("https://example.com/article", cache_dir=tmp_path)


class TestYoutubeRoutingAndResolution:
    def test_youtube_resolution_routes_to_youtube_fetcher(self):
        res = Resolution(url=_WATCH_URL, type="youtube", ingestable=True)
        assert get_fetcher(res.url, res) is YouTubeTranscriptFetcher

    def test_fetch_router_passes_cache_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            YouTubeTranscriptFetcher,
            "_get_client",
            lambda self: _FakeClient(segments=_SEGMENTS),
        )
        monkeypatch.setattr(
            YouTubeTranscriptFetcher, "_oembed_meta", lambda self, video_id: ("T", None)
        )
        res = Resolution(url=_WATCH_URL, type="youtube", ingestable=True)
        result = fetch(res.url, res, cache_dir=tmp_path)

        assert result.content_path == str(tmp_path / "youtube-abc123.md")
        assert (tmp_path / "youtube-abc123.md").exists()

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=abc123",
            "https://youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
        ],
    )
    def test_resolve_url_youtube(self, url):
        res = resolve_url(url)
        assert res.type == "youtube"
        assert res.ingestable is True

    def test_resolve_url_adjacent_rules_unchanged(self):
        """The new rule must not shadow arxiv/wikipedia/github/web/media."""
        assert resolve_url("https://arxiv.org/abs/2304.12345").type == "arxiv"
        assert resolve_url("https://en.wikipedia.org/wiki/Python_(programming_language)").type == "wikipedia"
        assert resolve_url("https://github.com/u/r/blob/main/f.py").type == "github_file"
        assert resolve_url("https://example.com/paper.pdf").type == "web"
        assert resolve_url("https://x.com/user/status/1").ingestable is False


# ── memex extract integration (in-process CLI, faked client) ──────

class TestExtractYoutube:
    def test_success_creates_extracted_node_with_cached_transcript(self, store, monkeypatch):
        data = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS)

        assert data["status"] == "extracted"
        assert data["fetcher_type"] == "youtube"
        assert data["confidence"] == "low"
        assert data["trust_state"] == "auto-verified"
        assert data["title"] == "Fake Video Title"

        cache = store["vault"] / ".cache" / "youtube-abc123.md"
        assert Path(data["content_path"]) == cache
        assert cache.exists()
        text = cache.read_text(encoding="utf-8")
        assert "This is the first segment" in text
        # The transcript body alone carries the node to auto-verified: no
        # injected '> Synthesis:' line (tickets #138/#140).
        assert "> Synthesis:" not in text

        con = sqlite3.connect(store["db"])
        con.row_factory = sqlite3.Row
        try:
            ext = con.execute(
                "SELECT * FROM node WHERE id = ?", (data["extracted_node_id"],)
            ).fetchone()
            src = con.execute(
                "SELECT * FROM source WHERE node_id = ?", (data["url_node_id"],)
            ).fetchone()
        finally:
            con.close()
        assert ext["kind"] == "extracted"
        assert ext["tier"] == "extracted"
        assert ext["depth"] == 1
        assert ext["fetcher_type"] == "youtube"
        assert ext["confidence"] == "low"
        assert ext["trust_state"] == "auto-verified"
        assert ext["content_path"] == str(cache)
        assert src["failed"] == 0
        assert src["title"] == "Fake Video Title"
        assert src["canonical_key"] == "youtube://abc123"
        assert _counts(store["db"]) == (1, 1, 1)

    def test_no_transcript_returns_no_content(self, store, monkeypatch):
        """A video without a transcript is an expected absence (ADR-0013,
        ticket #140): `memex extract` reports no_content — only the URL node
        + source are recorded, no extracted node, no metadata stub file."""
        data = _extract(
            store,
            monkeypatch,
            _WATCH_URL,
            error=TranscriptsDisabled("abc123"),
            oembed=("No Transcript Video", None),
        )

        assert data["status"] == "no_content"
        assert "extracted_node_id" not in data
        assert data["url_node_id"]
        assert data["title"] == "No Transcript Video"

        # URL node + source recorded; NO extracted node, no stub file, no cache.
        assert _counts(store["db"]) == (1, 0, 1)
        assert not list((store["vault"] / "extracted").glob("*.md"))
        assert list((store["vault"] / ".cache").glob("youtube-*.md")) == []

    def test_network_error_marks_source_failed(self, store, monkeypatch):
        data = _extract(store, monkeypatch, _WATCH_URL, error=RequestBlocked("abc123"))

        assert data["status"] == "fetch_failed"
        assert "url_node_id" in data
        assert "youtube transcript fetch failed" in data["error"]
        con = sqlite3.connect(store["db"])
        try:
            failed = con.execute(
                "SELECT failed FROM source WHERE node_id = ?", (data["url_node_id"],)
            ).fetchone()[0]
        finally:
            con.close()
        assert failed == 1
        assert _counts(store["db"]) == (1, 0, 1)

    def test_reextract_same_url_returns_already_exists(self, store, monkeypatch):
        data = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS)

        # The second extract dedups by canonical key before any fetch: a
        # client that would raise must never be called.
        monkeypatch.setattr(
            YouTubeTranscriptFetcher,
            "_get_client",
            lambda self: _FakeClient(error=RuntimeError("must not be called")),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), _WATCH_URL],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        second = json.loads(result.output)

        assert second["status"] == "already_exists"
        assert second["url_node_id"] == data["url_node_id"]
        assert second["extracted_node_id"] == data["extracted_node_id"]
        assert _counts(store["db"]) == (1, 1, 1)

    def test_force_reextract_uses_cache_without_calling_client(self, store, monkeypatch):
        data = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS)
        cache = store["vault"] / ".cache" / "youtube-abc123.md"
        original = cache.read_text(encoding="utf-8")

        # Re-extract with a client that would raise: the cache-first path must
        # not touch the client at all and must leave the immutable cache alone.
        monkeypatch.setattr(
            YouTubeTranscriptFetcher,
            "_get_client",
            lambda self: _FakeClient(error=RuntimeError("must not be called")),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["extract", "--force", "--db", str(store["db"]), "--vault", str(store["vault"]), _WATCH_URL],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)

        assert data["status"] == "re_extracted"
        assert data["fetcher_type"] == "youtube"
        assert data["trust_state"] == "auto-verified"
        assert data["title"] == "Fake Video Title"  # read back from the cache header
        assert Path(data["content_path"]) == cache
        assert cache.read_text(encoding="utf-8") == original  # immutable
        assert _counts(store["db"]) == (1, 1, 1)

    def test_force_reextract_with_transcript_creates_extracted_node(self, store, monkeypatch):
        """A no-transcript video (no_content, no extracted node) re-extracted
        once a transcript is available gets a fresh extracted node whose row
        content_path points at the cache artifact (ticket #140)."""
        first = _extract(
            store,
            monkeypatch,
            _WATCH_URL,
            error=TranscriptsDisabled("abc123"),
            oembed=("No Transcript Video", None),
        )
        assert first["status"] == "no_content"
        assert "extracted_node_id" not in first

        data = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS, force=True)

        assert data["status"] == "extracted"
        cache = store["vault"] / ".cache" / "youtube-abc123.md"
        assert Path(data["content_path"]) == cache
        assert cache.exists()
        con = sqlite3.connect(store["db"])
        con.row_factory = sqlite3.Row
        try:
            ext = con.execute(
                "SELECT content_path FROM node WHERE id = ?", (data["extracted_node_id"],)
            ).fetchone()
        finally:
            con.close()
        assert ext["content_path"] == str(cache)
        assert _counts(store["db"]) == (1, 1, 1)

    def test_force_no_transcript_never_writes_stub_file(self, store, monkeypatch):
        """A no-transcript (re-)extract writes nothing: no cache artifact, no
        vault/extracted stub (ticket #140). The node row is untouched — the
        deleted cache path is the documented manual refresh, and a later
        extract with a transcript recreates the cache artifact in place."""
        first = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS)
        ext_id = first["extracted_node_id"]
        cache = store["vault"] / ".cache" / "youtube-abc123.md"
        assert Path(first["content_path"]) == cache
        cache.unlink()  # documented refresh

        data = _extract(
            store,
            monkeypatch,
            _WATCH_URL,
            error=TranscriptsDisabled("abc123"),
            oembed=("No Transcript Video", None),
            force=True,
        )

        assert data["status"] == "no_content"
        # No metadata stub anywhere — not in .cache, not in vault/extracted.
        assert not cache.exists()
        assert list((store["vault"] / ".cache").glob("youtube-*.md")) == []
        assert not list((store["vault"] / "extracted").glob("*.md"))
        # The extracted node row is untouched (still points at the deleted
        # cache artifact).
        con = sqlite3.connect(store["db"])
        con.row_factory = sqlite3.Row
        try:
            ext = con.execute(
                "SELECT content_path FROM node WHERE id = ?", (ext_id,)
            ).fetchone()
        finally:
            con.close()
        assert ext["content_path"] == str(cache)
        assert _counts(store["db"]) == (1, 1, 1)

        # A later extract must call the API again (cache-first branch not
        # short-circuited): the transcript is now available, so the cache
        # artifact is recreated with real transcript content.
        again = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS, force=True)
        assert Path(again["content_path"]) == cache
        assert cache.exists()
        assert "This is the first segment" in cache.read_text(encoding="utf-8")
        assert _counts(store["db"]) == (1, 1, 1)


# ── derive on a youtube extracted node must not crash ─────────────

class TestDeriveYoutube:
    def _derive(self, store, node_id: str):
        return _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )

    def test_no_content_youtube_node_is_not_derivable(self, store, monkeypatch):
        """A video without a transcript leaves NO extracted L0 (ticket #140):
        derive --all finds nothing to derive and no summary node is created."""
        data = _extract(
            store,
            monkeypatch,
            _WATCH_URL,
            error=TranscriptsDisabled("abc123"),
            oembed=("No Transcript Video", None),
        )
        assert data["status"] == "no_content"
        assert "extracted_node_id" not in data

        proc = _run_memex(
            ["derive", "--all", "--db", str(store["db"]), "--vault", str(store["vault"])],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == []

        con = sqlite3.connect(store["db"])
        try:
            summary_count = con.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'summary'"
            ).fetchone()[0]
        finally:
            con.close()
        assert summary_count == 0

    def test_derive_transcript_youtube_node_does_not_crash(self, store, monkeypatch):
        data = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS)
        proc = self._derive(store, data["extracted_node_id"])
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
        assert d["status"] == "derived"
        assert d["l0_node_id"] == data["extracted_node_id"]
