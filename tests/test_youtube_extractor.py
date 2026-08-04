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
    def test_no_transcript_returns_metadata_only(self, tmp_path, monkeypatch, exc_cls):
        fetcher = YouTubeTranscriptFetcher(client=_FakeClient(error=exc_cls("abc123")))
        monkeypatch.setattr(
            YouTubeTranscriptFetcher, "_oembed_meta", lambda self, video_id: (None, None)
        )
        result = fetcher.fetch(_WATCH_URL, cache_dir=tmp_path)

        assert result.content_path is None
        assert result.title is None
        assert "transcript_available: false" in result.content
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
        assert "> Synthesis:" in text

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

    def test_no_transcript_returns_metadata_only(self, store, monkeypatch):
        data = _extract(
            store,
            monkeypatch,
            _WATCH_URL,
            error=TranscriptsDisabled("abc123"),
            oembed=("No Transcript Video", None),
        )

        assert data["status"] == "extracted"
        assert data["fetcher_type"] == "youtube"
        assert data["confidence"] == "low"
        assert data["trust_state"] == "draft"
        assert data["title"] == "No Transcript Video"

        md_path = Path(data["content_path"])
        assert md_path.parent == store["vault"] / "extracted"
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "video_id: abc123" in content
        assert "transcript_available: false" in content
        # No cache artifact was written.
        assert list((store["vault"] / ".cache").glob("youtube-*.md")) == []
        assert _counts(store["db"]) == (1, 1, 1)

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

    def test_force_reextract_with_transcript_updates_db_content_path(self, store, monkeypatch):
        """Finding 2: a metadata-only node re-extracted with a transcript now
        available must have its row content_path moved to the cache artifact —
        derive/render read the row, and a stale vault/extracted path would
        serve the metadata file forever."""
        first = _extract(
            store,
            monkeypatch,
            _WATCH_URL,
            error=TranscriptsDisabled("abc123"),
            oembed=("No Transcript Video", None),
        )
        assert Path(first["content_path"]).parent == store["vault"] / "extracted"

        data = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS, force=True)

        assert data["status"] == "re_extracted"
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

    def test_force_no_transcript_after_cache_delete_targets_extracted_dir(self, store, monkeypatch):
        """Finding 3: when the fetch writes no artifact, --force must target a
        fresh CLI-owned vault/extracted file — never the DB's previous cache
        path. Overwriting .cache/youtube-<id>.md with metadata would poison
        the immutable cache-first branch forever."""
        first = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS)
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

        assert data["status"] == "re_extracted"
        md_path = Path(data["content_path"])
        assert md_path.parent == store["vault"] / "extracted"
        assert md_path.exists()
        assert "transcript_available: false" in md_path.read_text(encoding="utf-8")
        # The cache file must NOT be recreated with metadata-only content.
        assert not cache.exists()
        assert list((store["vault"] / ".cache").glob("youtube-*.md")) == []
        # The node row tracks the new CLI-owned file.
        con = sqlite3.connect(store["db"])
        con.row_factory = sqlite3.Row
        try:
            ext = con.execute(
                "SELECT content_path FROM node WHERE id = ?", (data["extracted_node_id"],)
            ).fetchone()
        finally:
            con.close()
        assert ext["content_path"] == str(md_path)
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

    def test_derive_metadata_only_youtube_node_does_not_crash(self, store, monkeypatch):
        data = _extract(
            store,
            monkeypatch,
            _WATCH_URL,
            error=TranscriptsDisabled("abc123"),
            oembed=("No Transcript Video", None),
        )
        proc = self._derive(store, data["extracted_node_id"])
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
        assert d["status"] in ("derived", "error")
        assert d["l0_node_id"] == data["extracted_node_id"]

    def test_derive_transcript_youtube_node_does_not_crash(self, store, monkeypatch):
        data = _extract(store, monkeypatch, _WATCH_URL, segments=_SEGMENTS)
        proc = self._derive(store, data["extracted_node_id"])
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
        assert d["status"] == "derived"
        assert d["l0_node_id"] == data["extracted_node_id"]
