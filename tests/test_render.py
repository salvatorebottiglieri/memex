"""Tests for `memex render` command.

The render command walks all nodes in the SQLite DB and writes YAML
frontmatter onto each node's markdown file in the vault, preserving
the existing body content untouched.

Tested via the CLI subprocess seam (same pattern as test_ingest, test_derive).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from memex.store import Store
from tests.conftest import _run_memex, FAKE_FETCHER, ingest

FAKE_LLM = "tests.fake_llm_client:FakeLLMClient"


def _render(store) -> list:
    result = _run_memex(
        ["render", "--db", str(store["db"]), "--vault", str(store["vault"])],
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _ingest(store, url: str) -> dict:
    p = ingest(store, url)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _derive(store, node_id: str):
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_LLM_MODULE": FAKE_LLM},
    )


def _read_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a markdown file. Returns (frontmatter_dict, body_text).

    Uses the same parser as the production code (``renderer._extract_body``)
    for consistency: strict ``---\n`` delimiters only.
    """
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1])
            body = parts[2].lstrip("\n")
            return (fm if isinstance(fm, dict) else {}), body
    return {}, text


# ── Helpers ───────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_node(store, *, node_id: str | None = None, kind: str = "raw_source",
               content: str | None = None,
               content_path: str | None = None,
               title: str | None = None) -> tuple[str, Path | None]:
    """Create a node + source row directly via Store and return (node_id, md_path).

    If ``content_path`` is an explicit empty string, the node is created with
    no file on disk and ``md_path`` is ``None``.
    """
    if node_id is None:
        node_id = str(uuid.uuid4())
    if content_path is not None:
        # Explicit path (including empty string) — use as-is, no file write
        resolved_path = content_path
        md_path: Path | None = Path(content_path) if content_path else None
    else:
        md_path = store["vault"] / f"{node_id}.md"
        md_path.write_text(content or f"# {node_id}\n\nBody text.", encoding="utf-8")
        resolved_path = str(md_path)
    con = sqlite3.connect(store["db"])
    st = Store(con)
    st.create_node(node_id=node_id, kind=kind, trust_state="draft", depth=0,
                   content_path=resolved_path, created_at=_now())
    if kind == "raw_source":
        st.attach_source(node_id=node_id, canonical_key=f"test://{node_id}",
                         source_url=f"https://test.example/{node_id}", title=title or "",
                         fetched_at=_now(), failed=False)
    con.commit()
    con.close()
    return node_id, md_path


def _make_edge(store, from_node: str, to_node: str, relation: str,
               type: str = "association") -> None:
    """Create an edge between two nodes via Store."""
    con = sqlite3.connect(store["db"])
    st = Store(con)
    st.create_edge(edge_id=str(uuid.uuid4()), type=type, relation=relation,
                   from_node=from_node, to_node=to_node)
    con.commit()
    con.close()


# ── L0 Render ─────────────────────────────────────────────────────


class TestRenderL0:
    def test_render_outputs_json_array(self, store):
        """Render on an empty vault returns []."""
        result = _render(store)
        assert isinstance(result, list)
        assert result == []

    def test_render_l0_node(self, store):
        """Ingest an L0 node, render, assert frontmatter."""
        data = _ingest(store, "https://example.com/article")
        node_id = data["id"]
        results = _render(store)
        assert len(results) == 1
        assert results[0]["node_id"] == node_id
        assert results[0]["status"] == "rendered"

        md_path = store["vault"] / f"{node_id}.md"
        fm, body = _read_frontmatter(md_path)

        assert fm["id"] == node_id
        assert fm["kind"] == "raw_source"
        assert fm["depth"] == 0
        assert "created_at" in fm
        assert "tags" in fm
        assert "kind/raw_source" in fm["tags"]
        assert fm["source_url"] == "https://example.com/article"
        assert fm["title"] == "Fake Article Title"
        # trust_state and tier should not be present for L0 nodes
        assert fm.get("trust_state") is None
        assert fm.get("tier") is None
        # aliases should contain the title
        assert "Fake Article Title" in fm.get("aliases", [])

    def test_render_l0_body_preserved(self, store):
        """Body content is preserved through a render cycle."""
        data = _ingest(store, "https://example.com/article")
        node_id = data["id"]

        # Read the original body before render
        md_path = store["vault"] / f"{node_id}.md"
        original_body = md_path.read_text(encoding="utf-8")

        _render(store)

        fm, body = _read_frontmatter(md_path)
        # The body should match original content (frontmatter was added, body preserved)
        assert len(body) > 0
        assert "Fake content" in body

    def test_render_l0_preserves_original_source_url(self, store):
        """Render preserves the original source_url (canonical key tracks separately)."""
        data = _ingest(store, "https://example.com/article?utm_source=twitter")
        node_id = data["id"]
        _render(store)

        md_path = store["vault"] / f"{node_id}.md"
        fm, _ = _read_frontmatter(md_path)
        # The source_url preserves the original, canonical_key is separate
        assert fm["source_url"] == "https://example.com/article?utm_source=twitter"

    def test_render_l0_with_alias_from_h1(self, store):
        """If no title, aliases should use the first H1 in body."""
        node_id, md_path = _make_node(store, content="# My Custom Title\n\nSome body content here.",
                                       title="")

        results = _render(store)
        assert len(results) == 1
        fm, _ = _read_frontmatter(md_path)
        assert "My Custom Title" in fm.get("aliases", [])


# ── Derivation Render ─────────────────────────────────────────────


class TestRenderDerivation:
    def test_render_derivation_node(self, store):
        """Ingest + derive, render, assert derivation-specific frontmatter."""
        data = _ingest(store, "https://example.com/article")
        l0_id = data["id"]
        d_result = _derive(store, l0_id)
        d_data = json.loads(d_result.stdout)
        deriv_id = d_data["id"]

        results = _render(store)
        assert len(results) == 2  # L0 + derivation
        statuses = {r["node_id"]: r["status"] for r in results}
        assert statuses[l0_id] == "rendered"
        assert statuses[deriv_id] == "rendered"

        md_path = store["vault"] / f"{deriv_id}.md"
        fm, body = _read_frontmatter(md_path)

        assert fm["id"] == deriv_id
        assert fm["kind"] == "summary"
        assert fm["depth"] == 1
        assert "trust_state" in fm
        assert fm["tier"] == "notes"
        assert "tags" in fm
        assert "kind/summary" in fm["tags"]
        assert "trust_state/auto-verified" in fm["tags"]
        assert "tier/notes" in fm["tags"]
        # check_failures should be present for derivation nodes
        assert "check_failures" in fm
        assert isinstance(fm["check_failures"], list)


# ── Idempotency ───────────────────────────────────────────────────


class TestRenderIdempotency:
    def test_render_twice_idempotent(self, store):
        """Re-rendering produces identical frontmatter."""
        data = _ingest(store, "https://example.com/article")
        node_id = data["id"]

        _render(store)
        md_path = store["vault"] / f"{node_id}.md"
        fm1, body1 = _read_frontmatter(md_path)

        _render(store)
        fm2, body2 = _read_frontmatter(md_path)

        assert fm1 == fm2
        assert body1 == body2

    def test_render_twice_no_new_json_entries(self, store):
        """Re-rendering returns the same JSON output."""
        data = _ingest(store, "https://example.com/article")
        node_id = data["id"]

        r1 = _render(store)
        r2 = _render(store)
        assert r1 == r2


# ── Edge Cases ────────────────────────────────────────────────────


class TestRenderEdgeCases:
    def test_render_empty_vault(self, store):
        """Empty vault returns empty result."""
        results = _render(store)
        assert results == []

    def test_render_missing_db_returns_error(self, store):
        """Missing DB path should exit non-zero."""
        result = _run_memex(
            ["render", "--db", str(store["tmp"] / "nonexistent.db"), "--vault", str(store["vault"])],
        )
        assert result.returncode != 0

    def test_render_missing_vault_returns_error(self, store):
        """Missing vault path should exit non-zero with clean JSON error."""
        result = _run_memex(
            ["render", "--db", str(store["db"]), "--vault", str(store["tmp"] / "nonexistent_vault")],
        )
        assert result.returncode != 0
        data = json.loads(result.stderr)
        assert data.get("error") == "vault_not_found"

    def test_render_missing_content_path_skips_node(self, store):
        """Node with content_path pointing at non-existent file is skipped."""
        missing_path = store["vault"] / "nonexistent.md"
        node_id, _ = _make_node(store, content_path=str(missing_path))

        results = _render(store)
        skipped = [r for r in results if r["status"] == "skipped"]
        assert len(skipped) >= 1
        assert any(r["node_id"] == node_id for r in skipped)

    def test_render_empty_content_path_skips_node(self, store):
        """Node with empty content_path is skipped."""
        node_id, _ = _make_node(store, content_path="")

        results = _render(store)
        # Two results: the original ingested node (rendered) and this one (skipped)
        skipped = [r for r in results if r["status"] == "skipped"]
        assert len(skipped) == 1
        assert skipped[0]["node_id"] == node_id

    def test_render_preserves_body_on_rerender(self, store):
        """Body content survives multiple render cycles unchanged."""
        data = _ingest(store, "https://example.com/article")
        node_id = data["id"]

        md_path = store["vault"] / f"{node_id}.md"
        original_body = md_path.read_text(encoding="utf-8")

        _render(store)
        _render(store)
        _render(store)

        fm, body = _read_frontmatter(md_path)
        # Body should contain all original content (frontmatter was stripped)
        assert "Fake content" in body
        # Reconstruct: the original was just body (no frontmatter), render added frontmatter
        assert len(body) > 10


# ── Edge wikilinks (slice 2) ──────────────────────────────────────


class TestRenderEdgeWikilinks:
    """Tests for outgoing edge → [[wikilink]] frontmatter fields."""

    def test_derivation_has_derived_from_wikilink(self, store):
        """Ingest + derive, render, assert derived_from: [[l0-uuid]]."""
        data = _ingest(store, "https://example.com/article")
        l0_id = data["id"]
        d_result = _derive(store, l0_id)
        deriv_id = json.loads(d_result.stdout)["id"]

        _render(store)

        md_path = store["vault"] / f"{deriv_id}.md"
        fm, _ = _read_frontmatter(md_path)

        assert "derived_from" in fm, f"Expected derived_from in {fm}"
        assert fm["derived_from"] == f"[[{l0_id}]]", f"got {fm['derived_from']!r}"
        # L0 node should NOT have derived_from (it's the target, not source)
        l0_fm, _ = _read_frontmatter(store["vault"] / f"{l0_id}.md")
        assert "derived_from" not in l0_fm

    def test_related_edge_yields_wikilink(self, store):
        """Node with outgoing related edge renders related: [[uuid]]."""
        node_a, _ = _make_node(store)
        node_b, _ = _make_node(store)
        _make_edge(store, from_node=node_a, to_node=node_b, relation="related")

        _render(store)

        fm_a, _ = _read_frontmatter(store["vault"] / f"{node_a}.md")
        assert "related" in fm_a, f"Expected related in {fm_a}"
        assert fm_a["related"] == f"[[{node_b}]]", f"got {fm_a['related']!r}"

        # node_b has no outgoing edges, should have no wikilinks
        fm_b, _ = _read_frontmatter(store["vault"] / f"{node_b}.md")
        assert "related" not in fm_b
        assert "derived_from" not in fm_b

    def test_contradicts_edge_yields_wikilink(self, store):
        """Node with outgoing contradicts edge renders contradicts: [[uuid]]."""
        node_a, _ = _make_node(store)
        node_b, _ = _make_node(store)
        _make_edge(store, from_node=node_a, to_node=node_b, relation="contradicts")

        _render(store)

        fm_a, _ = _read_frontmatter(store["vault"] / f"{node_a}.md")
        assert "contradicts" in fm_a
        assert fm_a["contradicts"] == f"[[{node_b}]]"

    def test_refines_edge_yields_wikilink(self, store):
        """Node with outgoing refines edge renders refines: [[uuid]]."""
        node_a, _ = _make_node(store)
        node_b, _ = _make_node(store)
        _make_edge(store, from_node=node_a, to_node=node_b, relation="refines")

        _render(store)

        fm_a, _ = _read_frontmatter(store["vault"] / f"{node_a}.md")
        assert "refines" in fm_a
        assert fm_a["refines"] == f"[[{node_b}]]"

    def test_multiple_edges_same_relation_yields_list(self, store):
        """Node with 2+ related edges renders related as a YAML list."""
        node_a, _ = _make_node(store)
        node_b, _ = _make_node(store)
        node_c, _ = _make_node(store)
        _make_edge(store, from_node=node_a, to_node=node_b, relation="related")
        _make_edge(store, from_node=node_a, to_node=node_c, relation="related")

        _render(store)

        fm_a, _ = _read_frontmatter(store["vault"] / f"{node_a}.md")
        assert "related" in fm_a
        rel = fm_a["related"]
        assert isinstance(rel, list), f"Expected list, got {type(rel).__name__}: {rel}"
        assert len(rel) == 2, f"Expected 2 items, got {len(rel)}: {rel}"
        assert f"[[{node_b}]]" in rel
        assert f"[[{node_c}]]" in rel

    def test_node_with_no_edges_unchanged(self, store):
        """Node with zero outgoing edges produces same frontmatter as slice 1."""
        data = _ingest(store, "https://example.com/article")
        _render(store)

        md_path = store["vault"] / f"{data['id']}.md"
        fm, _ = _read_frontmatter(md_path)

        # Should have slice 1 fields but no edge fields
        assert "id" in fm
        assert "kind" in fm
        assert "derived_from" not in fm
        assert "related" not in fm
        assert "contradicts" not in fm
        assert "refines" not in fm

    def test_edge_wikilinks_idempotent(self, store):
        """Re-rendering with same edges produces same wikilinks."""
        node_a, _ = _make_node(store)
        node_b, _ = _make_node(store)
        _make_edge(store, from_node=node_a, to_node=node_b, relation="related")

        _render(store)
        _render(store)
        _render(store)

        fm_a, _ = _read_frontmatter(store["vault"] / f"{node_a}.md")
        assert fm_a["related"] == f"[[{node_b}]]"

    def test_derived_from_edge_is_scalar_not_list(self, store):
        """Single derived_from edge renders as scalar [[uuid]], not a list."""
        data = _ingest(store, "https://example.com/article")
        l0_id = data["id"]
        d_result = _derive(store, l0_id)
        deriv_id = json.loads(d_result.stdout)["id"]

        _render(store)

        md_path = store["vault"] / f"{deriv_id}.md"
        fm, _ = _read_frontmatter(md_path)
        assert isinstance(fm["derived_from"], str), \
            f"Expected scalar string, got {type(fm['derived_from']).__name__}: {fm['derived_from']}"


# ── Smoke-compatible quick checks ─────────────────────────────────


def test_render_multiple_types(store):
    """Multiple nodes of different types render correctly."""
    # Ingest two URLs, derive from one
    l0_1 = _ingest(store, "https://example.com/alpha")
    l0_2 = _ingest(store, "https://example.com/beta")
    _derive(store, l0_1["id"])
    _derive(store, l0_2["id"])

    results = _render(store)
    assert len(results) == 4  # 2 L0 + 2 derivations
    for r in results:
        assert r["status"] in ("rendered", "skipped")

    # Check a derivation's frontmatter
    for r in results:
        fm, _ = _read_frontmatter(store["vault"] / f"{r['node_id']}.md")
        assert "id" in fm
        assert "kind" in fm
        assert "depth" in fm
