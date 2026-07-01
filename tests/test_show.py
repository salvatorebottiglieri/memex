"""Tests for `memex show <id>` command.

show is strictly read-only — it must not write to db or filesystem.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from tests.conftest import ingest, _run_memex


def show(store, node_id: str):
    return _run_memex(
        ["show", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
    )


def test_show_returns_json_for_known_id(store):
    result = ingest(store, "https://example.com/article")
    ingested = json.loads(result.stdout)
    result2 = show(store, ingested["id"])
    assert result2.returncode == 0, result2.stderr
    data = json.loads(result2.stdout)
    assert data["id"] == ingested["id"]


def test_show_includes_content(store):
    result = ingest(store, "https://example.com/article")
    ingested = json.loads(result.stdout)
    result2 = show(store, ingested["id"])
    data = json.loads(result2.stdout)
    assert data["content"] is not None
    assert "Fake content" in data["content"]


def test_show_includes_canonical_key(store):
    result = ingest(store, "https://example.com/article?utm_source=test")
    ingested = json.loads(result.stdout)
    result2 = show(store, ingested["id"])
    data = json.loads(result2.stdout)
    assert data["canonical_key"] == "https://example.com/article"


def test_show_includes_source_url(store):
    url = "https://example.com/article"
    result = ingest(store, url)
    ingested = json.loads(result.stdout)
    result2 = show(store, ingested["id"])
    data = json.loads(result2.stdout)
    assert data["source_url"] == url


def test_show_includes_l0_path(store):
    result = ingest(store, "https://example.com/article")
    ingested = json.loads(result.stdout)
    result2 = show(store, ingested["id"])
    data = json.loads(result2.stdout)
    assert data["l0_path"] is not None
    assert Path(data["l0_path"]).exists()


def test_show_includes_trust_state(store):
    result = ingest(store, "https://example.com/article")
    ingested = json.loads(result.stdout)
    result2 = show(store, ingested["id"])
    data = json.loads(result2.stdout)
    assert data["trust_state"] == "draft"


def test_show_returns_error_for_unknown_id(store):
    result = show(store, "00000000-0000-0000-0000-000000000000")
    assert result.returncode == 1
    data = json.loads(result.stdout)
    assert data["error"] == "not_found"


def test_show_does_not_write_to_db(store):
    result = ingest(store, "https://example.com/article")
    ingested = json.loads(result.stdout)
    mtime_before = os.path.getmtime(store["db"])
    time.sleep(0.05)
    show(store, ingested["id"])
    mtime_after = os.path.getmtime(store["db"])
    assert mtime_before == mtime_after
