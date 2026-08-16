"""Tests for `memex synthesize <id1> <id2> ...`.

Agent is injected via MEMEX_AGENT — no real Anthropic calls.
The fake agent module lives at tests/fake_llm_client.py.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from memex.store import Store
from tests.conftest import _run_memex, register_node


FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_THROWS_AGENT = "tests.fake_llm_client_throws:FakeLLMClientThrows"


def _ingest(store, url: str) -> dict:
    filename = url.rsplit("/", 1)[-1].split("?", 1)[0] + ".md"
    result = register_node(store, store["vault"], filename, url)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _synthesize(store, *node_ids: str) -> subprocess.CompletedProcess:
    return _run_memex(
        ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), *node_ids],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )


class TestSynthesize:
    def test_synthesize_returns_json_with_synthesis_id(self, store):
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "synthesized"
        assert isinstance(data["id"], str)
        assert len(data["id"]) > 0

    def test_synthesize_node_fields(self, store):
        """The synthesis node has kind=summary, tier=synthesis, depth=parent.depth+1."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        conn = sqlite3.connect(store["db"])
        try:
            row = conn.execute(
                "SELECT kind, tier, depth FROM node WHERE id = ?", (data["id"],)
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "Synthesis node not found in store"
        kind, tier, depth = row
        assert kind == "summary"
        assert tier == "synthesis"
        # Registered nodes are extracted (depth=1), so max + 1 = 2
        assert depth == 2

    def test_synthesize_provenance_edges(self, store):
        """Synthesis creates N derived_from edges, one per parent."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        c = _ingest(store, "https://example.com/article-c")
        result = _synthesize(store, a["id"], b["id"], c["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        conn = sqlite3.connect(store["db"])
        try:
            edges = conn.execute(
                """
                SELECT to_node FROM edge
                WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
                ORDER BY to_node
                """,
                (data["id"],),
            ).fetchall()
        finally:
            conn.close()

        parent_ids = sorted([a["id"], b["id"], c["id"]])
        edge_targets = sorted(r[0] for r in edges)
        assert edge_targets == parent_ids

    def test_synthesize_writes_markdown_file(self, store):
        """Synthesis writes a .md file with derivation prose."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        md_path = Path(data.get("content_path", str(store["vault"] / f"{data['id']}.md")))
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "> Synthesis:" in content

    def test_synthesize_includes_parent_ids(self, store):
        """Response includes the list of parent node ids."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["parent_ids"] == [a["id"], b["id"]]

    def test_synthesize_is_idempotent(self, store):
        """Same set of parent ids produces already_synthesized on second call."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        r1 = _synthesize(store, a["id"], b["id"])
        assert r1.returncode == 0, r1.stderr
        d1 = json.loads(r1.stdout)
        assert d1["status"] == "synthesized"

        # Reverse order — should still match same unordered set
        r2 = _synthesize(store, b["id"], a["id"])
        assert r2.returncode == 0, r2.stderr
        d2 = json.loads(r2.stdout)
        assert d2["status"] == "already_synthesized"
        assert d2["id"] == d1["id"]

        # Verify only one node and N edges exist
        conn = sqlite3.connect(store["db"])
        try:
            node_count = conn.execute(
                "SELECT COUNT(*) FROM node WHERE id = ?", (d1["id"],)
            ).fetchone()[0]
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM edge WHERE from_node = ?", (d1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert node_count == 1
        assert edge_count == 2

    def test_synthesize_unknown_parent_returns_error(self, store):
        """Unknown parent node returns error."""
        result = _synthesize(store, "does-not-exist")
        assert result.returncode == 1
        data = json.loads(result.stderr)
        assert data["error"] == "error"
        assert "does-not-exist" in data.get("detail", "")

    def test_synthesize_single_parent(self, store):
        """Synthesize with a single parent is a valid edge case."""
        a = _ingest(store, "https://example.com/article-a")
        result = _synthesize(store, a["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "synthesized"
        assert len(data["parent_ids"]) == 1
        assert data["parent_ids"][0] == a["id"]

        # Should still be tier=synthesis; depth = extracted parent depth + 1 = 2
        conn = sqlite3.connect(store["db"])
        try:
            row = conn.execute(
                "SELECT kind, tier, depth FROM node WHERE id = ?", (data["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "summary"
        assert row[1] == "synthesis"
        assert row[2] == 2

    def test_synthesize_agent_failure_returns_error(self, store):
        """When the agent raises, the CLI returns error with exit code 1."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _run_memex(
            ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), a["id"], b["id"]],
            env={"MEMEX_AGENT": FAKE_THROWS_AGENT},
        )
        assert result.returncode != 0
        data = json.loads(result.stderr)
        assert data["error"] == "error"
        assert "detail" in data

    def test_synthesize_multiple_parents_depth_calculation(self, store):
        """Depth is max(parent.depth) + 1, not just 1."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")

        # Synthesize a and b (extracted, depth=1) to create a node with depth=2
        r1 = _synthesize(store, a["id"], b["id"])
        assert r1.returncode == 0, r1.stderr
        d1 = json.loads(r1.stdout)

        # Now synthesize the first synthesis + one extracted → depth=max(2,1)+1 = 3
        r2 = _synthesize(store, d1["id"], a["id"])
        assert r2.returncode == 0, r2.stderr
        d2 = json.loads(r2.stdout)
        assert d2["status"] == "synthesized"

        conn = sqlite3.connect(store["db"])
        try:
            row = conn.execute(
                "SELECT depth FROM node WHERE id = ?", (d2["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == 3, f"Expected depth 3, got {row[0]}"


class TestSynthesizeQualityGate:
    """Integration tests for the adversarial validation gate in _do_synthesize.

    Validator is injected via MEMEX_VALIDATOR env var.
    """

    FAKE_VALIDATOR_FAILS = "tests.fake_validator_fails:FakeValidatorFails"
    FAKE_VALIDATOR_WARNS = "tests.fake_validator_warns:FakeValidatorWarns"

    def test_no_validator_proceeds(self, store):
        """No MEMEX_VALIDATOR set -> synthesize proceeds normally (no regression)."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _synthesize(store, a["id"], b["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "synthesized"

    def test_fake_agent_validator_skips(self, store):
        """MEMEX_VALIDATOR=FakeAgent (no call_llm) -> validation skipped, synthesize proceeds."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _run_memex(
            ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), a["id"], b["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT, "MEMEX_VALIDATOR": FAKE_AGENT},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "synthesized"

    def test_failing_validator_rejects(self, store):
        """Validator rejects -> quality_failed, no node or edge created."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _run_memex(
            ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), a["id"], b["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT, "MEMEX_VALIDATOR": self.FAKE_VALIDATOR_FAILS},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "quality_failed"
        assert "Synthesis does not meaningfully re-elaborate" in data["reason"]
        assert a["id"] in data["parent_ids"]
        assert b["id"] in data["parent_ids"]
        # Verify no synthesis node was created
        conn = sqlite3.connect(store["db"])
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'synthesis'"
            ).fetchone()[0]
            assert count == 0, f"Expected 0 synthesis nodes, got {count}"
        finally:
            conn.close()

    def test_warning_validator_proceeds_with_warning(self, store):
        """Validator warns -> synthesize proceeds but warning on stderr."""
        a = _ingest(store, "https://example.com/article-a")
        b = _ingest(store, "https://example.com/article-b")
        result = _run_memex(
            ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), a["id"], b["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT, "MEMEX_VALIDATOR": self.FAKE_VALIDATOR_WARNS},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "synthesized"
        # Warning should be on stderr
        warning = json.loads(result.stderr.strip())
        assert "validator_warning" in warning
        assert "Validator LLM call failed" in warning["validator_warning"]


class TestSynthesizeCanonicalizeMarkers:
    """Ticket #143 — synthesis writes files + stores synthesis_statements with
    the same pattern as derive, so the file markers get the same
    canonicalization from the column (D3 passes → auto-verified)."""

    def test_synthesize_divergent_markers_are_canonicalized(self, store):
        """Prose markers with divergent quoting → written file markers equal the
        column exactly → D3 passes → auto-verified."""
        import re
        import uuid

        from memex.services.synthesize import SynthesizerService
        from tests.fake_llm_client import FakeAgentDivergent

        def _ingest(url: str) -> str:
            filename = f"{uuid.uuid4().hex}.md"
            p = register_node(store, store["vault"], filename, url)
            assert p.returncode == 0, p.stderr
            return json.loads(p.stdout)["id"]

        a = _ingest("https://example.com/article-a")
        b = _ingest("https://example.com/article-b")
        agent = FakeAgentDivergent(
            prose=(
                "# Synthesized Claim\n\n"
                "This synthesis aggregates the topic across both sources.\n\n"
                "> Synthesis: The 'x' claim\n\n"
                "Both source materials cover the subject thoroughly."
            ),
            statements=['The "x" claim'],
        )
        with Store.open(store["db"]) as s:
            result = SynthesizerService(
                s, Path(store["vault"]), agent
            ).synthesize([a, b])

        assert result["status"] == "synthesized"
        assert result["trust_state"] == "auto-verified"
        assert result["check_failures"] == []
        content = Path(result["content_path"]).read_text(encoding="utf-8")
        assert re.findall(r"> Synthesis:\s*(.*)", content) == ['The "x" claim']

        con = sqlite3.connect(store["db"])
        try:
            row = con.execute(
                "SELECT synthesis_statements FROM node WHERE id = ?",
                (result["id"],),
            ).fetchone()
        finally:
            con.close()
        assert json.loads(row[0]) == ['The "x" claim']


class TestBackfillSynthesis:
    """Tests for `memex backfill-synthesis` — synthesis_statements backfill.

    The candidate filter is kind-aware (``kind in ('summary', 'synthesis')``
    only). Regression: extracted/legacy raw_source files that merely contain a
    literal ``'> Synthesis:'`` line must never be backfilled — that would
    mislabel raw content as inference statements.
    """

    def test_backfill_only_populates_summary_and_synthesis_kinds(self, store):
        """Backfill scans summary/synthesis nodes only; url/extracted/
        raw_source nodes with marker lines stay untouched."""
        con = sqlite3.connect(store["db"])
        st = Store(con)
        now = datetime.now(timezone.utc).isoformat()
        vault = Path(store["vault"])

        def _mk(kind, filename, content, **kw) -> str:
            path = vault / filename
            path.write_text(content, encoding="utf-8")
            node_id = filename.replace(".md", "")
            st.create_node(node_id=node_id, kind=kind, content_path=str(path),
                           created_at=now, **kw)
            return node_id

        url_id = "url-node"
        st.create_node(node_id=url_id, kind="url", created_at=now)

        # extracted content containing a literal marker line — must NOT backfill
        extracted_id = _mk(
            "extracted", "extracted-node.md",
            "# Extracted\n\nBody mentions:\n> Synthesis: not a real statement\n",
            derived_from=url_id,
        )
        # legacy raw_source with its own source row — must NOT backfill
        raw_id = _mk(
            "raw_source", "legacy-raw.md",
            "# Legacy\n\n> Synthesis: legacy marker\n",
        )
        st.attach_source(node_id=raw_id, canonical_key="test://legacy-raw",
                         source_url="https://test.example/legacy-raw",
                         title="Legacy Raw", fetched_at=now)
        # derivation kinds — MUST backfill
        summary_id = _mk(
            "summary", "summary-node.md",
            "# Summary\n\n> Synthesis: Key point one\n> Synthesis: Key point two\n",
            tier="notes", depth=2,
        )
        synth_id = _mk(
            "synthesis", "synthesis-node.md",
            "# Synthesis\n\n> Synthesis: Cross-source claim\n",
            tier="synthesis", depth=2,
        )
        # summary without markers — scanned, no update
        nomarker_id = _mk(
            "summary", "summary-nomarker.md",
            "# Summary\n\nNo marker here.\n", tier="notes", depth=2,
        )
        con.commit()
        con.close()

        result = _run_memex(
            ["backfill-synthesis", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        by_id = {r["id"]: r for r in data["results"]}

        # candidates: the 3 summary/synthesis nodes with an existing file
        assert data["scanned"] == 3
        assert by_id[summary_id]["status"] == "updated"
        assert by_id[synth_id]["status"] == "updated"
        assert by_id[nomarker_id]["status"] == "no_marker_found"
        # url/extracted/raw_source are never scanned
        assert url_id not in by_id
        assert extracted_id not in by_id
        assert raw_id not in by_id

        # DB: only the two derivation nodes carry synthesis_statements
        con = sqlite3.connect(store["db"])
        try:
            rows = {
                r[0]: r[1] for r in con.execute(
                    "SELECT id, synthesis_statements FROM node"
                ).fetchall()
            }
        finally:
            con.close()
        assert json.loads(rows[summary_id]) == ["Key point one", "Key point two"]
        assert json.loads(rows[synth_id]) == ["Cross-source claim"]
        assert rows[extracted_id] is None
        assert rows[raw_id] is None
        assert rows[url_id] is None
        assert rows[nomarker_id] is None

    def test_backfill_dry_run_writes_nothing(self, store):
        """--dry-run reports would_update but leaves the DB untouched."""
        con = sqlite3.connect(store["db"])
        st = Store(con)
        now = datetime.now(timezone.utc).isoformat()
        path = Path(store["vault"]) / "summary-dry.md"
        path.write_text("# Summary\n\n> Synthesis: Dry run claim\n", encoding="utf-8")
        st.create_node(node_id="summary-dry", kind="summary", tier="notes", depth=2,
                       content_path=str(path), created_at=now)
        con.commit()
        con.close()

        result = _run_memex(
            ["backfill-synthesis", "--dry-run",
             "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["dry_run"] is True
        assert data["results"][0]["status"] == "would_update"

        con = sqlite3.connect(store["db"])
        try:
            ss = con.execute(
                "SELECT synthesis_statements FROM node WHERE id = 'summary-dry'"
            ).fetchone()[0]
        finally:
            con.close()
        assert ss is None
