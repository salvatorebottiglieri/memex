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
    register_node(store, store["vault"], "article.md", "https://example.com/article")
    result = _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert len(data) == 1


def test_list_node_has_required_fields(store):
    register_node(store, store["vault"], "article.md", "https://example.com/article")
    result = _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    node = json.loads(result.stdout)[0]
    assert "id" in node
    assert node["kind"] == "raw_source"
    assert node["tier"] is None
    assert node["trust_state"] == "draft"
    assert node["canonical_key"] == "https://example.com/article"


def test_list_returns_multiple_nodes(store):
    register_node(store, store["vault"], "article-1.md", "https://example.com/article-1")
    register_node(store, store["vault"], "article-2.md", "https://example.com/article-2")
    result = _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    data = json.loads(result.stdout)
    assert len(data) == 2


def test_list_does_not_write_to_db(store):
    """list is read-only: db mtime should not change after list."""
    register_node(store, store["vault"], "article.md", "https://example.com/article")
    mtime_before = os.path.getmtime(store["db"])
    time.sleep(0.05)
    _run_memex(["list", "--db", str(store["db"]), "--vault", str(store["vault"])])
    mtime_after = os.path.getmtime(store["db"])
    assert mtime_before == mtime_after
