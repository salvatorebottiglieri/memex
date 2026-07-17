"""Subprocess tests for memex resolve CLI."""
from __future__ import annotations

import json

from tests.conftest import _run_memex


def test_resolve_arxiv(tmp_path):
    proc = _run_memex(["resolve", "https://arxiv.org/abs/2304.12345"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["type"] == "arxiv"
    assert data["direct_url"] == "https://arxiv.org/pdf/2304.12345"


def test_resolve_web_article(tmp_path):
    proc = _run_memex(["resolve", "https://example.com/article"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["type"] == "web"
    assert data["ingestable"] is True


def test_resolve_missing_url(tmp_path):
    proc = _run_memex(["resolve"])
    assert proc.returncode != 0
    data = json.loads(proc.stderr)
    assert "error" in data


def test_resolve_github_blob(tmp_path):
    proc = _run_memex(["resolve", "https://github.com/user/repo/blob/main/file.py"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["type"] == "github_file"
    assert data["direct_url"] == "https://raw.githubusercontent.com/user/repo/main/file.py"


def test_resolve_wikipedia(tmp_path):
    proc = _run_memex(["resolve", "https://en.wikipedia.org/wiki/Python_(programming_language)"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["type"] == "wikipedia"
    assert data["direct_url"] == "https://en.wikipedia.org/api/rest_v1/page/summary/Python_(programming_language)"
