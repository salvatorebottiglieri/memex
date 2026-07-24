"""Tests for the memex register command."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.conftest import _run_memex, WORKTREE


def _register(store: dict, filename: str, source_url: str, extra_env: dict | None = None):
    """Write a markdown file with frontmatter and run memex register."""
    content = (
        f"---\nsource_url: {source_url}\ntitle: Test Article\n---\n\n"
        f"# Test Article\n\n"
        f"This is a longer article body that exceeds the minimum character threshold "
        f"of one hundred characters so that the L0 markdown file gets created in tests."
    )
    path = Path(store["vault"]) / filename
    path.write_text(content, encoding="utf-8")
    return _run_memex(
        ["register", "--db", str(store["db"]), "--vault", str(store["vault"]), str(path)],
        cwd=WORKTREE,
        env=extra_env,
    )


class TestRegister:
    def test_register_creates_node(self, store):
        """Registering a file creates a node + source row in the DB."""
        result = _register(store, "test.md", "https://example.com/article")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["status"] == "registered"
        assert data["canonical_key"] == "https://example.com/article"

        # Verify DB has the node
        con = sqlite3.connect(store["db"])
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id, kind, depth, content_path FROM node WHERE id = ?", (data["id"],)).fetchone()
        assert row is not None
        assert row["kind"] == "raw_source"
        assert row["depth"] == 0

        source = con.execute("SELECT node_id, canonical_key, source_url, title FROM source WHERE node_id = ?", (data["id"],)).fetchone()
        assert source is not None
        assert source["canonical_key"] == "https://example.com/article"
        assert source["source_url"] == "https://example.com/article"
        assert source["title"] == "Test Article"

    def test_register_idempotent(self, store):
        """Registering the same source_url twice yields already_exists."""
        r1 = _register(store, "a.md", "https://example.com/dup")
        assert r1.returncode == 0
        assert json.loads(r1.stdout)["status"] == "registered"

        r2 = _register(store, "b.md", "https://example.com/dup")
        assert r2.returncode == 0
        data = json.loads(r2.stdout)
        assert data["status"] == "already_exists"
        assert data["canonical_key"] == "https://example.com/dup"

    def test_register_missing_source_url_fails(self, store):
        """A markdown file without source_url in frontmatter must fail."""
        path = Path(store["vault"]) / "no-source.md"
        path.write_text("# Just a heading\n\nNo frontmatter.", encoding="utf-8")
        result = _run_memex(
            ["register", "--db", str(store["db"]), "--vault", str(store["vault"]), str(path)],
            cwd=WORKTREE,
        )
        assert result.returncode != 0
        detail = json.loads(result.stderr)
        assert "missing_source_url" in detail.get("error", "")

    def test_register_requires_existing_file(self, store):
        """Passing a non-existent path must fail."""
        path = Path(store["vault"]) / "nonexistent.md"
        result = _run_memex(
            ["register", "--db", str(store["db"]), "--vault", str(store["vault"]), str(path)],
            cwd=WORKTREE,
        )
        assert result.returncode != 0  # click reports usage error

    def test_register_override_source_url(self, store):
        """--source-url flag overrides frontmatter."""
        content = "---\nsource_url: https://frontmatter.example.com\n---\n\n# Override test\n\nBody."
        path = Path(store["vault"]) / "override.md"
        path.write_text(content, encoding="utf-8")
        result = _run_memex(
            [
                "register", "--db", str(store["db"]), "--vault", str(store["vault"]),
                str(path), "--source-url", "https://override.example.com",
            ],
            cwd=WORKTREE,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["status"] == "registered"
        assert data["canonical_key"] == "https://override.example.com"
