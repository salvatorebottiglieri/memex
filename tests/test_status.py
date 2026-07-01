"""Tests for `memex status` command."""
import json
from pathlib import Path

import pytest


def test_status_returns_json_with_paths_and_flags_when_initialized(tmp_path, run_memex):
    """memex status returns JSON with db_path, vault_path, and existence flags after init."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"

    run_memex(["init", "--db", str(db_path), "--vault", str(vault_path)])
    result = run_memex(["status", "--db", str(db_path), "--vault", str(vault_path)])

    assert result.returncode == 0, result.stderr

    data = json.loads(result.stdout)
    assert data["db_path"] == str(db_path)
    assert data["vault_path"] == str(vault_path)
    assert data["db_exists"] is True
    assert data["vault_exists"] is True


def test_status_reports_false_when_not_initialized(tmp_path, run_memex):
    """memex status reports existence flags as False when paths do not exist."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"

    result = run_memex(["status", "--db", str(db_path), "--vault", str(vault_path)])

    assert result.returncode == 0, result.stderr

    data = json.loads(result.stdout)
    assert data["db_exists"] is False
    assert data["vault_exists"] is False
