"""Tests for the memex register command."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memex.store import Store
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
    def test_register_creates_url_extracted_pair(self, store):
        """Registering a file creates URL-node + extracted-node + provenance edge."""
        result = _register(store, "test.md", "https://example.com/article")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["status"] == "registered"
        assert data["canonical_key"] == "https://example.com/article"
        # Backward-compat "id" points at the content-bearing (extracted) node
        assert data["id"] == data["extracted_node_id"]
        url_id = data["url_node_id"]
        extracted_id = data["extracted_node_id"]

        con = sqlite3.connect(store["db"])
        con.row_factory = sqlite3.Row

        # URL node: kind='url', depth=0, zero content, no trust/confidence
        url_row = con.execute(
            "SELECT id, kind, tier, trust_state, depth, content_path, confidence "
            "FROM node WHERE id = ?", (url_id,)
        ).fetchone()
        assert url_row is not None
        assert url_row["kind"] == "url"
        assert url_row["depth"] == 0
        assert url_row["tier"] is None
        assert url_row["trust_state"] is None
        assert url_row["content_path"] is None
        assert url_row["confidence"] is None

        # Source row binds to the URL node
        source = con.execute(
            "SELECT node_id, canonical_key, source_url, title FROM source WHERE node_id = ?",
            (url_id,),
        ).fetchone()
        assert source is not None
        assert source["canonical_key"] == "https://example.com/article"
        assert source["source_url"] == "https://example.com/article"
        assert source["title"] == "Test Article"

        # Extracted node: kind='extracted', tier='extracted', depth=1, content_path = file
        ex_row = con.execute(
            "SELECT id, kind, tier, trust_state, depth, content_path, confidence "
            "FROM node WHERE id = ?", (extracted_id,)
        ).fetchone()
        assert ex_row is not None
        assert ex_row["kind"] == "extracted"
        assert ex_row["tier"] == "extracted"
        assert ex_row["depth"] == 1
        assert ex_row["trust_state"] == "draft"
        # Manual registration has no fetcher -> confidence stays unset
        assert ex_row["confidence"] is None
        assert Path(ex_row["content_path"]) == Path(store["vault"]) / "test.md"

        # Extracted node has NO source row
        ex_source = con.execute(
            "SELECT node_id FROM source WHERE node_id = ?", (extracted_id,)
        ).fetchone()
        assert ex_source is None

        # Provenance edge: extracted -> URL-node
        edge = con.execute(
            "SELECT type, relation, from_node, to_node FROM edge "
            "WHERE from_node = ? AND to_node = ?",
            (extracted_id, url_id),
        ).fetchone()
        assert edge is not None
        assert edge["type"] == "provenance"
        assert edge["relation"] == "derived_from"
        con.close()

    def test_register_keeps_file_in_place(self, store):
        """The registered file stays exactly where the user placed it, untouched."""
        path = Path(store["vault"]) / "keep.md"
        content = (
            "---\nsource_url: https://example.com/keep\ntitle: Keep Me\n---\n\n"
            "# Keep Me\n\nBody of the file as the user wrote it."
        )
        path.write_text(content, encoding="utf-8")
        result = _run_memex(
            ["register", "--db", str(store["db"]), "--vault", str(store["vault"]), str(path)],
            cwd=WORKTREE,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["content_path"] == str(path)
        # File untouched: same content, same location
        assert path.exists()
        assert path.read_text(encoding="utf-8") == content

    def test_register_idempotent(self, store):
        """Registering the same source_url twice yields already_exists, no dupes."""
        r1 = _register(store, "a.md", "https://example.com/dup")
        assert r1.returncode == 0
        d1 = json.loads(r1.stdout)
        assert d1["status"] == "registered"

        r2 = _register(store, "b.md", "https://example.com/dup")
        assert r2.returncode == 0
        data = json.loads(r2.stdout)
        assert data["status"] == "already_exists"
        assert data["canonical_key"] == "https://example.com/dup"
        # Same pair of ids returned on dedup
        assert data["id"] == d1["extracted_node_id"]
        assert data["url_node_id"] == d1["url_node_id"]
        assert data["extracted_node_id"] == d1["extracted_node_id"]

        con = sqlite3.connect(store["db"])
        n = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        s = con.execute("SELECT COUNT(*) FROM source").fetchone()[0]
        e = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()
        assert n == 2  # one url + one extracted, no duplicates
        assert s == 1  # one source row
        assert e == 1  # one provenance edge

    def test_register_does_not_touch_existing_raw_source_nodes(self, store):
        """Existing raw_source nodes (expand phase) are left untouched by register."""
        # Seed a legacy raw_source node + source row directly
        raw_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with Store.open(store["db"]) as s:
            s.create_node(
                node_id=raw_id, kind="raw_source", depth=0,
                content_path="", created_at=now,
            )
            s.attach_source(
                node_id=raw_id, canonical_key="https://legacy.example.com/x",
                source_url="https://legacy.example.com/x", fetched_at=now,
            )

        # Register a NEW source_url — must not disturb the raw_source node
        result = _register(store, "new.md", "https://example.com/new")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert json.loads(result.stdout)["status"] == "registered"

        con = sqlite3.connect(store["db"])
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id, kind, depth, content_path FROM node WHERE id = ?", (raw_id,)
        ).fetchone()
        assert row is not None
        assert row["kind"] == "raw_source"
        assert row["depth"] == 0
        src = con.execute(
            "SELECT node_id, canonical_key, source_url FROM source WHERE node_id = ?", (raw_id,)
        ).fetchone()
        assert src is not None
        assert src["canonical_key"] == "https://legacy.example.com/x"
        # No provenance edge was attached to the legacy node
        edge = con.execute(
            "SELECT id FROM edge WHERE to_node = ? AND relation = 'derived_from'", (raw_id,)
        ).fetchone()
        assert edge is None
        con.close()

    def test_register_dedup_with_legacy_raw_source(self, store):
        """Re-registering a legacy raw_source URL reports already_exists with
        no extracted id (there is no pair) and leaves it untouched."""
        raw_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with Store.open(store["db"]) as s:
            s.create_node(
                node_id=raw_id, kind="raw_source", depth=0,
                content_path="", created_at=now,
            )
            s.attach_source(
                node_id=raw_id, canonical_key="https://legacy.example.com/y",
                source_url="https://legacy.example.com/y", fetched_at=now,
            )

        result = _register(store, "legacy.md", "https://legacy.example.com/y")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["status"] == "already_exists"
        assert data["url_node_id"] == raw_id
        assert data["extracted_node_id"] is None
        assert data["id"] == raw_id  # backward-compat id = the existing node

        con = sqlite3.connect(store["db"])
        n = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        s = con.execute("SELECT COUNT(*) FROM source").fetchone()[0]
        e = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()
        assert n == 1  # no new nodes
        assert s == 1  # no duplicate source row
        assert e == 0  # no edges created

    def test_register_dedup_with_derived_legacy_raw_source(self, store):
        """A DERIVED legacy raw_source must not be mistaken for a registered
        pair: it has a derived_from edge (to its summary), but already_exists
        still reports extracted_node_id=null and id=url_id."""
        raw_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with Store.open(store["db"]) as s:
            s.create_node(
                node_id=raw_id, kind="raw_source", depth=0,
                content_path="", created_at=now,
            )
            s.attach_source(
                node_id=raw_id, canonical_key="https://legacy.example.com/z",
                source_url="https://legacy.example.com/z", fetched_at=now,
            )
            # The legacy pipeline derived a summary from this raw_source
            summary_id = str(uuid.uuid4())
            s.create_node(
                node_id=summary_id, kind="summary", tier="notes",
                trust_state="draft", depth=1, content_path="",
                created_at=now,
            )
            s.create_edge(
                edge_id=str(uuid.uuid4()), type="provenance",
                relation="derived_from", from_node=summary_id, to_node=raw_id,
            )

        result = _register(store, "legacy.md", "https://legacy.example.com/z")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["status"] == "already_exists"
        assert data["url_node_id"] == raw_id
        assert data["extracted_node_id"] is None
        assert data["id"] == raw_id  # backward-compat id = the existing node
        # The summary is NOT surfaced as the pair's extracted node
        assert data["extracted_node_id"] != summary_id

        con = sqlite3.connect(store["db"])
        n = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        s = con.execute("SELECT COUNT(*) FROM source").fetchone()[0]
        e = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()
        assert n == 2  # raw_source + summary, no new nodes
        assert s == 1  # no duplicate source row
        assert e == 1  # no new edges

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
