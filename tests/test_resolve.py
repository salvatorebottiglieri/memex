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
