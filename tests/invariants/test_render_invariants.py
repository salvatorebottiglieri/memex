"""Core invariants — render step (ADR-0008: one-way, DB -> markdown).

    V1  render is idempotent: running it twice produces byte-identical files
    V2  render preserves the body: only frontmatter is written/rewritten,
        the body content of every node file is untouched
    V3  every rendered file carries the core frontmatter fields
        (id, kind, depth, trust_state, tier)
    V4  render succeeds (exit 0) on an empty vault and on a derived campus
    V5  provenance is surfaced in frontmatter: derived nodes carry a
        derived_from wikilink to their parent

Obsidian is view-only (ADR-0008): render never edits the DB and never
deletes or rewrites node bodies.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from tests.conftest import _run_memex, register_node

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"


def _render(store) -> subprocess.CompletedProcess:
    return _run_memex(
        ["render", "--db", str(store["db"]), "--vault", str(store["vault"])],
    )


def _vault_files(store) -> dict[Path, str]:
    return {
        p: p.read_text(encoding="utf-8")
        for p in sorted(store["vault"].rglob("*.md"))
        if ".memex" not in p.parts
    }


def _derive(store, node_id: str):
    result = _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestRenderIdempotence:
    def test_render_twice_is_byte_identical(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        _derive(store, reg["id"])

        assert _render(store).returncode == 0
        first = _vault_files(store)
        assert _render(store).returncode == 0
        second = _vault_files(store)

        assert first.keys() == second.keys()
        for path in first:
            assert first[path] == second[path], f"render not idempotent for {path}"

    def test_render_on_empty_vault_succeeds(self, store):
        assert _render(store).returncode == 0


class TestRenderPreservesBody:
    def test_body_untouched_by_render(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        _derive(store, reg["id"])

        def _bodies() -> dict[Path, str]:
            bodies = {}
            for p in store["vault"].rglob("*.md"):
                if ".memex" in p.parts:
                    continue
                text = p.read_text(encoding="utf-8")
                # V2 — compare body content, ignoring the frontmatter
                # separator: render normalizes the body to start on the line
                # after a blank line (matching the L0 register format).
                if text.startswith("---"):
                    body = text.split("---", 2)[2].lstrip("\n")
                else:
                    body = text.lstrip("\n")
                bodies[p] = body
            return bodies

        before = _bodies()
        assert _render(store).returncode == 0
        after = _bodies()

        assert before == after, "render modified node bodies"


class TestFrontmatterContent:
    def test_core_fields_present(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        derived = _derive(store, reg["id"])
        assert _render(store).returncode == 0

        by_id = {}
        for p in store["vault"].rglob("*.md"):
            if ".memex" in p.parts:
                continue
            text = p.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            fm = yaml.safe_load(text.split("---", 2)[1])
            by_id[fm.get("id")] = (fm, text)

        # V3 — core fields on every rendered node; the url node is a DB-only
        # ledger row (no content file, ADR-0008) and is intentionally absent.
        for node_id in (reg["id"], derived["id"]):
            assert node_id in by_id, f"node {node_id} not rendered"
            fm, _ = by_id[node_id]
            for field in ("id", "kind", "depth", "trust_state", "tier"):
                assert field in fm, f"node {node_id} missing frontmatter field {field}"
        assert reg["url_node_id"] not in by_id

        # V5 — the summary carries a derived_from wikilink
        summary_fm, _ = by_id[derived["id"]]
        assert "derived_from" in summary_fm
        assert reg["id"] in summary_fm["derived_from"]
