"""Tests for `memex version` and `memex --version`.

The version must come from installed package metadata (importlib.metadata),
never a hardcoded string — assert against the metadata so a stale number
fails loudly.
"""
from __future__ import annotations

import importlib.metadata
import json

from tests.conftest import _run_memex


def test_version_command_emits_json_from_metadata():
    result = _run_memex(["version"])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {"version": importlib.metadata.version("memex")}


def test_version_flag_prints_prog_and_version():
    result = _run_memex(["--version"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"memex {importlib.metadata.version('memex')}"
