"""Tests for `memex sync` command."""
import json
import os
import shlex
from pathlib import Path



def test_sync_commits_and_outputs_json(tmp_path, run_memex):
    """memex sync renders, commits, and outputs JSON."""
    # Init a git repo + memex vault
    db = tmp_path / "memex.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".gitkeep").write_text("")

    result = run_memex(["init", "--db", str(db), "--vault", str(vault)])
    assert result.returncode == 0, result.stderr

    # git init + initial commit so there's a HEAD
    os.system(f"git -C {shlex.quote(str(vault))} init -q")
    os.system(f"git -C {shlex.quote(str(vault))} add -A")
    os.system(f"git -C {shlex.quote(str(vault))} commit -q -m init")

    # Run sync
    result = run_memex(["sync", "--db", str(db), "--vault", str(vault), "--no-push"])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "rendered" in data
    assert "committed" in data
    assert data["pushed"] is False


def test_sync_install_hooks(tmp_path, run_memex):
    """memex sync --install-hooks writes a post-merge hook."""
    db = tmp_path / "memex.db"
    vault = tmp_path / "vault"
    vault.mkdir()

    result = run_memex(["init", "--db", str(db), "--vault", str(vault)])
    assert result.returncode == 0, result.stderr

    os.system(f"git -C {shlex.quote(str(vault))} init -q")

    result = run_memex(["sync", "--db", str(db), "--vault", str(vault), "--install-hooks"])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "hook_installed" in data

    hook = Path(data["hook_installed"])
    assert hook.exists()
    assert hook.stat().st_mode & 0o111  # executable
    text = hook.read_text()
    assert "memex render" in text
    assert str(vault) in text
