"""Tests for `memex list` command.

list is strictly read-only — it must not write to db or filesystem.
"""
from __future__ import annotations

import json
import os
import time

from tests.conftest import _run_memex, register_node


def test_list_returns_empty_array_when_no_nodes(store):
    result = _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == []


def test_list_returns_array_with_one_node_after_ingest(store):
    """register produces a url + extracted pair; list hides the URL-node by default."""
    register_node(store, store["vault"], "article.md", "https://example.com/article")
    result = _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data) == 1
    assert data[0]["kind"] == "extracted"


def test_list_kind_url_shows_url_nodes(store):
    """The URL-node (source metadata carrier) is visible with --kind url."""
    register_node(store, store["vault"], "article.md", "https://example.com/article")
    result = _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"]),
                         "--kind", "url"])
    node = json.loads(result.stdout)[0]
    assert "id" in node
    assert node["kind"] == "url"
    assert node["tier"] is None
    assert node["trust_state"] is None  # URL-nodes have no trust state
    assert node["canonical_key"] == "https://example.com/article"


def test_list_returns_multiple_nodes(store):
    """Each registered file yields one visible node (extracted); URL-nodes hidden."""
    register_node(store, store["vault"], "article-1.md", "https://example.com/article-1")
    register_node(store, store["vault"], "article-2.md", "https://example.com/article-2")
    result = _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    data = json.loads(result.stdout)
    assert len(data) == 2
    assert {n["kind"] for n in data} == {"extracted"}


def test_list_does_not_write_to_db(store):
    """list is read-only: db mtime should not change after list."""
    register_node(store, store["vault"], "article.md", "https://example.com/article")
    mtime_before = os.path.getmtime(store["db"])
    time.sleep(0.05)
    _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    mtime_after = os.path.getmtime(store["db"])
    assert mtime_before == mtime_after


def test_list_confidence_unset(store):
    """Manually registered nodes have NULL confidence — reachable via --confidence unset."""
    register_node(store, store["vault"], "article.md", "https://example.com/article")
    result = _run_memex(
        ["list", "--db", str(store["db"]), "--vault", str(store["vault"]),
         "--confidence", "unset"]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data) == 1  # the extracted node (URL-node hidden by default)
    assert data[0]["confidence"] is None
