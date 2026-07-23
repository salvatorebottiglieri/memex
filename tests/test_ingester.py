"""Unit tests for memex.ingester — no subprocess, no network.

Tests ``ingest_single_url`` directly via the Store seam, running in
~1ms instead of ~1s for the subprocess equivalent.
"""
from __future__ import annotations

from pathlib import Path

from memex.fetcher import FetchResult
from memex.ingester import ingest_single_url
from memex.store import Store


class FakeFetcher:
    """Minimal inline fetcher — no conftest dependency needed."""

    def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            content=(f"# Content for {url}\n\n"
                     f"This is a longer body that exceeds the minimum character threshold "
                     f"of one hundred characters so that the L0 markdown file is created."),
            title="Test Title",
        )


def test_ingest_single_url_returns_ingested(tmp_path):
    """Direct call to ingest_single_url produces an ingested result."""
    db_path = tmp_path / "test.db"
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    with Store.open(db_path) as store:
        store.init_schema()
        result = ingest_single_url(store, vault_path, "https://example.com/article", FakeFetcher())

    assert result["status"] == "ingested"
    assert result["canonical_key"] == "https://example.com/article"


def test_ingest_single_url_already_exists(tmp_path):
    """Same URL ingested twice returns already_exists."""
    db_path = tmp_path / "test.db"
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    with Store.open(db_path) as store:
        store.init_schema()
        ingest_single_url(store, vault_path, "https://example.com/article", FakeFetcher())
        result2 = ingest_single_url(store, vault_path, "https://example.com/article", FakeFetcher())

    assert result2["status"] == "already_exists"


def test_ingest_single_url_writes_markdown(tmp_path):
    """L0 markdown file is written to the vault."""
    db_path = tmp_path / "test.db"
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    with Store.open(db_path) as store:
        store.init_schema()
        result = ingest_single_url(store, vault_path, "https://example.com/article", FakeFetcher())

    md_path = Path(result.get("content_path", str(vault_path / f"{result['id']}.md")))
    assert md_path.exists()
    assert "Content for" in md_path.read_text()


def test_ingest_single_url_pdf_url(tmp_path):
    """Ingesting a .pdf URL works through the pipeline with a fake fetcher."""
    db_path = tmp_path / "test.db"
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    with Store.open(db_path) as store:
        store.init_schema()
        result = ingest_single_url(
            store, vault_path, "https://example.com/paper.pdf", FakeFetcher()
        )

    assert result["status"] == "ingested"
    assert result["canonical_key"] == "https://example.com/paper.pdf"
    md_path = Path(result.get("content_path", str(vault_path / f"{result['id']}.md")))
    assert md_path.exists()
    assert "Content for" in md_path.read_text()
    assert "content_path" in result


def test_ingest_single_url_youtube_metadata_only(tmp_path):
    """YouTube path: metadata-only content (< 100 chars) with content_path set."""
    db_path = tmp_path / "test.db"
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    # Fake YouTube fetcher: short metadata content + content_path to cache
    cache_file = tmp_path / "youtube-abc123.md"
    cache_file.write_text("Transcript text here.\n", encoding="utf-8")

    class FakeYouTubeFetcher:
        def fetch(self, url: str):
            return FetchResult(
                content="# My YouTube Video\nChannel: Test Channel",
                title="My YouTube Video",
                content_path=str(cache_file),
            )

    with Store.open(db_path) as store:
        store.init_schema()
        result = ingest_single_url(
            store, vault_path, "https://www.youtube.com/watch?v=abc123", FakeYouTubeFetcher()
        )
    assert result["status"] == "ingested"
    assert result["canonical_key"] == "youtube://abc123"
    assert result["content_path"] is not None
    # L0 mirrors the fetcher cache into the vault root so Obsidian can index it.
    assert result["content_path"].endswith("my-youtube-video.md")
    # The mirror lives in the vault root, not in any subfolder.
    assert Path(result["content_path"]).parent == vault_path
    # The original cache file is preserved (L0 is immutable at the fetcher level).
    assert cache_file.exists()
    assert "Transcript" in cache_file.read_text()
    # The mirror's content matches the cache.
    assert "Transcript" in Path(result["content_path"]).read_text()
