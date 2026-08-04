"""Tests for ontology generation and CLI.

Single source of truth: render_ontology() must match the checked-in file.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from memex.cli import cli
from memex.rules import render_ontology


def _find_project_root() -> Path:
    """Walk up from the test file directory looking for pyproject.toml."""
    cwd = Path(__file__).resolve().parent
    for parent in [cwd] + list(cwd.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    return cwd


PROJECT_ROOT = _find_project_root()


def test_ontology_is_identical_to_registry():
    """Generated ontology matches the checked-in file. If this fails, run `memex ontology`."""
    generated = render_ontology()
    current = (PROJECT_ROOT / "docs/ONTOLOGY.md").read_text(encoding="utf-8")
    assert generated == current, (
        "docs/ONTOLOGY.md is out of sync with the Rule registry — "
        "run `uv run memex ontology` to regenerate"
    )


def test_ontology_cli_writes_file(tmp_path):
    """Running `memex ontology` writes the file and reports 'written'."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        (root / "pyproject.toml").write_text("")
        docs_dir = root / "docs"
        docs_dir.mkdir()
        (docs_dir / "ONTOLOGY.md").write_text("# stale\n", encoding="utf-8")

        result = runner.invoke(cli, ["ontology"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "written"
        written = (docs_dir / "ONTOLOGY.md").read_text(encoding="utf-8")
        assert len(written) > 10
        assert "## 1. Entity model" in written


def test_ontology_cli_check_passes_when_identical(tmp_path):
    """--check exits 0 when the file matches the registry."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        (root / "pyproject.toml").write_text("")
        docs_dir = root / "docs"
        docs_dir.mkdir()
        generated = render_ontology()
        (docs_dir / "ONTOLOGY.md").write_text(generated, encoding="utf-8")

        result = runner.invoke(cli, ["ontology", "--check"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "identical"


def test_ontology_cli_check_fails_when_drifted(tmp_path):
    """--check exits 1 when the file differs from the registry."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        (root / "pyproject.toml").write_text("")
        docs_dir = root / "docs"
        docs_dir.mkdir()
        (docs_dir / "ONTOLOGY.md").write_text("# wrong content\n", encoding="utf-8")

        result = runner.invoke(cli, ["ontology", "--check"], catch_exceptions=False)
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "drifted"
