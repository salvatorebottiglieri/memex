"""Tests for ticket #98 — CLI reshape for the new kinds (url / extracted).

Covers the four viewing surfaces against a url+extracted+summary graph
seeded directly via Store (no register/derive subprocesses), plus one
legacy raw_source node to pin the transition behavior:

  list   — url nodes hidden by default; ``--kind url`` returns them;
           ``--tier extracted`` filters correctly
  show   — url node: metadata only (no content/trust/confidence);
           extracted: full view + tier/fetcher_type/confidence/url_parent_id
  render — url node produces no file; extracted node renders L0-style
           frontmatter with source_url/title from the URL parent's source row
  stats  — roots counted as kind='url'; coverage over extracted nodes;
           legacy rows group under their own kind key

The store-level contract is also pinned here (middle-out: data layer first):
``list_nodes(kind=None)`` excludes url nodes so CLI limit/offset pagination
stays correct, and ``get_stats`` measures roots/coverage over the new kinds.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

import yaml

from memex.store import Store
from tests.conftest import _run_memex, _store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(store) -> dict:
    """Create a url + extracted + summary graph via Store directly, plus one
    legacy raw_source node to pin the transition behavior (legacy rows still
    list/render/group under their own kind).

    Returns a dict of node ids plus the extracted node's content path.
    """
    con = sqlite3.connect(store["db"])
    st = Store(con)

    url_id = str(uuid.uuid4())
    st.create_node(node_id=url_id, kind="url")
    st.attach_source(
        node_id=url_id,
        canonical_key="https://example.com/article",
        source_url="https://example.com/article",
        title="Example Article",
        fetched_at=_now(),
        failed=False,
    )

    ext_id = str(uuid.uuid4())
    ext_path = store["vault"] / f"{ext_id}.md"
    ext_path.write_text("# Extracted Body\n\nExtracted content.\n", encoding="utf-8")
    st.create_node(
        node_id=ext_id, kind="extracted", fetcher_type="http",
        content_path=str(ext_path), derived_from=url_id,
    )

    summary_id = str(uuid.uuid4())
    summary_path = store["vault"] / f"{summary_id}.md"
    summary_path.write_text("# Notes\n\nSummarised content.\n", encoding="utf-8")
    st.create_node(
        node_id=summary_id, kind="summary", tier="notes", trust_state="draft",
        depth=1, content_path=str(summary_path), created_at=_now(),
    )
    st.create_edge(
        edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
        from_node=summary_id, to_node=url_id,
    )

    raw_id = str(uuid.uuid4())
    raw_path = store["vault"] / f"{raw_id}.md"
    raw_path.write_text("# Raw\n\nLegacy content.\n", encoding="utf-8")
    st.create_node(
        node_id=raw_id, kind="raw_source", trust_state="draft", depth=0,
        content_path=str(raw_path), created_at=_now(),
    )
    st.attach_source(
        node_id=raw_id, canonical_key="test://raw",
        source_url="https://test.example/raw", title="Raw Source",
        fetched_at=_now(), failed=False,
    )

    con.commit()
    con.close()
    return {
        "url": url_id,
        "extracted": ext_id,
        "summary": summary_id,
        "raw_source": raw_id,
        "extracted_path": ext_path,
    }


def _memex(store, cmd, *args) -> dict | list:
    """Run a CLI command against the store fixture and parse its JSON output."""
    result = _run_memex(
        [cmd, "--db", str(store["db"]), "--vault", str(store["vault"]), *args],
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _read_frontmatter(path) -> tuple[dict, str]:
    """Parse YAML frontmatter + body from a markdown file."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            fm = yaml.safe_load(parts[1])
            body = parts[2].lstrip("\n")
            return (fm if isinstance(fm, dict) else {}), body
    return {}, text


# ── list ─────────────────────────────────────────────────────────────


class TestListReshape:
    def test_list_default_excludes_url_nodes(self, store):
        ids = _seed(store)
        nodes = _memex(store, "list")
        listed = {n["id"] for n in nodes}
        assert ids["url"] not in listed
        assert {ids["extracted"], ids["summary"], ids["raw_source"]} <= listed
        assert len(nodes) == 3

    def test_list_kind_url_returns_url_nodes(self, store):
        ids = _seed(store)
        nodes = _memex(store, "list", "--kind", "url")
        assert [n["id"] for n in nodes] == [ids["url"]]
        assert nodes[0]["kind"] == "url"

    def test_list_tier_extracted_filters(self, store):
        ids = _seed(store)
        nodes = _memex(store, "list", "--tier", "extracted")
        assert [n["id"] for n in nodes] == [ids["extracted"]]
        assert nodes[0]["kind"] == "extracted"
        assert nodes[0]["tier"] == "extracted"


# ── show ─────────────────────────────────────────────────────────────


class TestShowReshape:
    def test_show_url_node_is_metadata_only(self, store):
        ids = _seed(store)
        data = _memex(store, "show", ids["url"])
        assert data["id"] == ids["url"]
        assert data["kind"] == "url"
        assert data["depth"] == 0
        assert data["canonical_key"] == "https://example.com/article"
        assert data["source_url"] == "https://example.com/article"
        assert data["title"] == "Example Article"
        assert set(data["children"]) == {ids["extracted"], ids["summary"]}
        # Metadata only: no content, trust, confidence, tier or file fields.
        assert set(data.keys()) == {
            "id", "kind", "depth", "created_at",
            "canonical_key", "source_url", "title", "children",
        }

    def test_show_extracted_includes_full_view_plus_new_fields(self, store):
        ids = _seed(store)
        data = _memex(store, "show", ids["extracted"])
        assert data["id"] == ids["extracted"]
        assert data["kind"] == "extracted"
        assert data["tier"] == "extracted"
        assert data["depth"] == 1
        assert data["fetcher_type"] == "http"
        assert data["confidence"] == "medium"  # EXTRACTED_CONFIDENCE[http]
        assert data["url_parent_id"] == ids["url"]
        assert data["content"] is not None
        assert "Extracted Body" in data["content"]
        assert data["l0_path"] == str(ids["extracted_path"])


# ── render ───────────────────────────────────────────────────────────


class TestRenderReshape:
    def test_render_url_node_produces_no_file(self, store):
        ids = _seed(store)
        results = _memex(store, "render")
        by_id = {r["node_id"]: r for r in results}
        # URL node has no content_path: never rendered, never written to disk.
        assert ids["url"] not in by_id
        assert all(r["status"] == "rendered" for r in results)
        assert len(results) == 3  # extracted + summary + raw_source

    def test_render_extracted_uses_url_parent_source(self, store):
        ids = _seed(store)
        _memex(store, "render")

        fm, body = _read_frontmatter(ids["extracted_path"])
        assert fm["kind"] == "extracted"
        assert fm["tier"] == "extracted"
        assert "trust_state" in fm
        # source_url/title come from the URL parent's source row, not the
        # extracted node itself (which carries no source row).
        assert fm["source_url"] == "https://example.com/article"
        assert fm["title"] == "Example Article"
        assert "kind/extracted" in fm["tags"]
        assert "tier/extracted" in fm["tags"]
        # L0-style aliases inherit the parent title.
        assert "Example Article" in fm.get("aliases", [])
        # Derived_from wikilink targets the URL parent (no content_path ->
        # falls back to the parent's id as filename).
        assert fm["derived_from"] == f"[[{ids['url']}|Example Article]]"
        assert "Extracted content." in body


# ── stats ────────────────────────────────────────────────────────────


class TestStatsReshape:
    def test_stats_counts_url_roots_and_extracted_coverage(self, store):
        ids = _seed(store)
        data = _memex(store, "stats")
        assert data["roots"] == 1
        assert data["by_kind"]["url"] == 1
        assert data["by_kind"]["extracted"] == 1
        # URL nodes (tier NULL) group under their own tier key; legacy
        # raw_source rows group under their own kind key too.
        assert data["by_tier"]["url"] == 1
        assert data["by_tier"]["extracted"] == 1
        assert data["by_tier"]["raw_source"] == 1
        # Coverage: derived extracted / total extracted (1/1 here).
        assert data["derivation_coverage_pct"] == 100.0


# ── store layer (middle-out) ─────────────────────────────────────────


class TestStoreListNodesDefault:
    def test_list_nodes_default_excludes_url(self):
        store = _store()
        url_id = str(uuid.uuid4())
        store.create_node(node_id=url_id, kind="url")
        ext_id = str(uuid.uuid4())
        store.create_node(
            node_id=ext_id, kind="extracted", fetcher_type="http",
            content_path="/tmp/e.md", derived_from=url_id,
        )
        raw_id = str(uuid.uuid4())
        store.create_node(node_id=raw_id, kind="raw_source")

        assert {n["id"] for n in store.list_nodes()} == {ext_id, raw_id}
        assert [n["id"] for n in store.list_nodes(kind="url")] == [url_id]
        assert [n["id"] for n in store.list_nodes(kind="extracted")] == [ext_id]

    def test_list_nodes_default_exclude_respects_pagination(self):
        store = _store()
        url_id = str(uuid.uuid4())
        store.create_node(node_id=url_id, kind="url", created_at="2024-01-05T00:00:00")
        for i in range(4):
            store.create_node(
                node_id=f"n{i}", kind="raw_source",
                created_at=f"2024-01-0{i + 1}T00:00:00",
            )
        page1 = store.list_nodes(limit=2)
        page2 = store.list_nodes(limit=2, offset=2)
        assert len(page1) == 2 and len(page2) == 2
        ids = [n["id"] for n in page1 + page2]
        assert ids == ["n0", "n1", "n2", "n3"]
        assert url_id not in ids


class TestStoreStatsReshape:
    def test_stats_roots_and_coverage_over_extracted(self):
        store = _store()
        url_id = str(uuid.uuid4())
        store.create_node(node_id=url_id, kind="url")
        e1 = str(uuid.uuid4())
        store.create_node(
            node_id=e1, kind="extracted", content_path="/tmp/e1.md", derived_from=url_id,
        )
        e2 = str(uuid.uuid4())
        store.create_node(
            node_id=e2, kind="extracted", content_path="/tmp/e2.md", derived_from=url_id,
        )
        stats = store.get_stats()
        assert stats["roots"] == 1
        assert stats["derivation_coverage_pct"] == 100.0
        assert stats["by_tier"]["url"] == 1
        assert stats["by_tier"]["extracted"] == 2

    def test_stats_coverage_partial_when_extracted_edge_missing(self):
        store = _store()
        url_id = str(uuid.uuid4())
        store.create_node(node_id=url_id, kind="url")
        e1 = str(uuid.uuid4())
        store.create_node(
            node_id=e1, kind="extracted", content_path="/tmp/e1.md", derived_from=url_id,
        )
        e2 = str(uuid.uuid4())
        store.create_node(
            node_id=e2, kind="extracted", content_path="/tmp/e2.md", derived_from=url_id,
        )
        # Remove e2's provenance edge -> coverage drops to 50%.
        store._con.execute("DELETE FROM edge WHERE from_node = ?", (e2,))
        stats = store.get_stats()
        assert stats["roots"] == 1
        assert stats["derivation_coverage_pct"] == 50.0

    def test_stats_no_url_roots(self):
        store = _store()
        store.create_node(node_id="n1", kind="raw_source", tier=None)
        stats = store.get_stats()
        assert stats["roots"] == 0
        assert stats["derivation_coverage_pct"] == 0.0
        # Legacy rows group under their own kind key (COALESCE(tier, kind)
        # grouping is data-driven — no hardcoded kind assumptions).
        assert stats["by_tier"] == {"raw_source": 1}
