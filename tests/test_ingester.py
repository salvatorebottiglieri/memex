"""Unit tests for memex.ingester — no subprocess, no network.

Tests ``ingest_single_url`` directly via the Store seam, running in
~1ms instead of ~1s for the subprocess equivalent.
"""
from __future__ import annotations

import sqlite3
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

    md_path = vault_path / f"{result['id']}.md"
    assert md_path.exists()
    assert "Content for" in md_path.read_text()
