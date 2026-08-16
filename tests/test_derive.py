"""Tests for `memex derive <node-id>` and `memex search <query>`.

Agent is injected via MEMEX_AGENT — no real Anthropic calls.
The fake agent module lives at tests/fake_llm_client.py.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memex.store import Store as _Store
from tests.conftest import _run_memex, register_node


FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_FAILING_AGENT = "tests.fake_llm_client_failing:FakeLLMClientFailing"
FAKE_THROWS_AGENT = "tests.fake_llm_client_throws:FakeLLMClientThrows"



def _derive(store, node_id: str) -> subprocess.CompletedProcess:
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )


def _seed_raw_source(store: dict, filename: str, source_url: str) -> dict:
    """Create a legacy raw_source L0 directly via the Store (transition phase).

    ``memex register`` now produces url+extracted pairs, while the derive /
    checks pipeline still processes legacy raw_source nodes (depth-0 L0s) —
    seed those fixtures directly so the derive tests keep exercising that path.
    """
    node_id = str(uuid.uuid4())
    md_path = Path(store["vault"]) / filename
    md_path.write_text(
        f"---\nsource_url: {source_url}\ntitle: Test Article\n---\n\n"
        f"# Test Article\n\n"
        f"This is a longer article body that exceeds the minimum character threshold "
        f"of one hundred characters so that the L0 markdown file gets created in tests.",
        encoding="utf-8",
    )
    con = sqlite3.connect(store["db"])
    st = _Store(con)
    st.create_node(
        node_id=node_id, kind="raw_source", depth=0,
        content_path=str(md_path), created_at=datetime.now(timezone.utc).isoformat(),
    )
    st.attach_source(
        node_id=node_id, canonical_key=source_url,
        source_url=source_url, title="Test Article", fetched_at=None,
    )
    con.commit()
    con.close()
    return {"id": node_id}


def _seed_raw_source_short(store: dict, filename: str, source_url: str) -> dict:
    """Create a legacy raw_source L0 whose content is below MIN_CHARS.

    Mirrors the real-vault shape (55-byte frontmatter-only files) that used
    to produce process-description notes (ticket #141).
    """
    node_id = str(uuid.uuid4())
    md_path = Path(store["vault"]) / filename
    md_path.write_text(
        f"---\nsource_url: {source_url}\ntitle: Short\n---\n",
        encoding="utf-8",
    )
    assert len(md_path.read_text(encoding="utf-8")) < 100
    con = sqlite3.connect(store["db"])
    st = _Store(con)
    st.create_node(
        node_id=node_id, kind="raw_source", depth=0,
        content_path=str(md_path), created_at=datetime.now(timezone.utc).isoformat(),
    )
    st.attach_source(
        node_id=node_id, canonical_key=source_url,
        source_url=source_url, title="Short", fetched_at=None,
    )
    con.commit()
    con.close()
    return {"id": node_id}


class TestDerive:
    def test_derive_returns_json_with_derivation_id(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        result = _derive(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert "id" in data
        assert data["status"] == "derived"

    def test_derive_inserts_notes_tier_node(self, store):
        """The derivation node has kind=summary, tier=notes, depth=1.

        FakeAgent produces a valid derivation (has synthesis marker, right length),
        so trust_state is auto-verified after checks run.
        """
        ingested = _seed_raw_source(store, "test.md", "https://example.com/article")
        result = _derive(store, ingested["id"])
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT kind, tier, trust_state, depth FROM node WHERE id = ?", (deriv_id,)
        ).fetchone()
        con.close()

        assert row is not None
        kind, tier, trust_state, depth = row
        assert kind == "summary"
        assert tier == "notes"
        assert trust_state == "auto-verified"
        assert depth == 1

    def test_derive_from_extracted_root_auto_verifies(self, store):
        """Notes derived from an extracted root (depth 1) land at depth 2 and
        auto-verify (D5 accepts parent depth + 1, ticket #103)."""
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        result = _derive(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "derived"
        assert data["trust_state"] == "auto-verified"
        assert data["check_failures"] == []

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT kind, tier, trust_state, depth FROM node WHERE id = ?",
            (data["id"],),
        ).fetchone()
        con.close()

        assert row is not None
        kind, tier, trust_state, depth = row
        assert kind == "summary"
        assert tier == "notes"
        assert trust_state == "auto-verified"
        assert depth == 2

    def test_derive_inserts_provenance_edge(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        l0_id = ingested["id"]
        result = _derive(store, l0_id)
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT type, relation, from_node, to_node, written_by FROM edge "
            "WHERE from_node = ? AND to_node = ?",
            (deriv_id, l0_id),
        ).fetchone()
        con.close()

        assert row is not None
        assert row[0] == "provenance"
        assert row[1] == "derived_from"
        assert row[2] == deriv_id
        assert row[3] == l0_id
        # LLM-created provenance edges carry written_by='llm' (not the default 'human')
        assert row[4] == "llm"

    def test_derive_writes_markdown_file_with_synthesis_markers(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        result = _derive(store, ingested["id"])
        data = json.loads(result.stdout)
        md_path = Path(data["content_path"])
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "> Synthesis:" in content

    def test_derive_response_includes_l0_node_id(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        result = _derive(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["l0_node_id"] == ingested["id"]

    def test_derive_is_idempotent(self, store):
        """Deriving the same L0 twice produces one summary node and one edge."""
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        l0_id = ingested["id"]

        first = _derive(store, l0_id)
        assert first.returncode == 0, first.stderr
        first_data = json.loads(first.stdout)
        assert first_data["status"] == "derived"

        second = _derive(store, l0_id)
        assert second.returncode == 0, second.stderr
        second_data = json.loads(second.stdout)
        assert second_data["status"] == "already_derived"

        con = sqlite3.connect(store["db"])
        node_count = con.execute(
            "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
        ).fetchone()[0]
        edge_count = con.execute(
            "SELECT COUNT(*) FROM edge WHERE to_node = ? "
            "AND type = 'provenance' AND relation = 'derived_from'",
            (l0_id,),
        ).fetchone()[0]
        con.close()
        assert node_count == 1
        assert edge_count == 1

    def test_derive_unknown_node_returns_error(self, store):
        result = _derive(store, "does-not-exist")
        assert result.returncode != 0
        data = json.loads(result.stderr)
        assert data["error"] == "error"
        assert data["detail"] == "node_not_found"


class TestDeriveAll:
    """Tests for memex derive --all with --limit."""

    def _derive_all(self, store, limit: int | None = None, agent: str = FAKE_AGENT):
        args = [
            "derive", "--db", str(store["db"]),
            "--vault", str(store["vault"]), "--all",
        ]
        if limit is not None:
            args.extend(["--limit", str(limit)])
        return _run_memex(args, env={"MEMEX_AGENT": agent})

    def _ingest_n(self, store, n: int, prefix: str = "article") -> list[dict]:
        """Create n legacy raw_source L0 nodes directly (derive --all
        processes both extracted roots and legacy raw_source rows) and
        return their result dicts."""
        results = []
        for i in range(n):
            results.append(
                _seed_raw_source(store, f"{prefix}-{i}.md",
                                 f"https://example.com/{prefix}-{i}")
            )
        return results

    def _register_n(self, store, n: int, prefix: str = "pair") -> list[dict]:
        """Register n files — each yields a url+extracted pair (current model).

        The returned dicts use ``id`` = the extracted node id (the
        content-bearing L0 of the pair).
        """
        results = []
        for i in range(n):
            vault = Path(store["vault"])
            p = register_node(store, vault, f"{prefix}-{i}.md",
                              f"https://example.com/{prefix}-{i}")
            assert p.returncode == 0, p.stderr
            results.append(json.loads(p.stdout))
        return results

    def test_derive_all_targets_extracted_roots(self, store):
        """derive --all derives the extracted roots of url+extracted pairs.

        Summary lands at depth=2 (extracted is depth 1, parent_depth + 1) with
        a provenance edge back to the extracted node. D5 accepts notes at
        parent depth + 1 (ticket #103), so the derivations auto-verify.
        """
        pairs = self._register_n(store, 3)
        extracted_ids = {p["id"] for p in pairs}

        result = self._derive_all(store)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 3
        assert all(r["status"] == "derived" for r in data)
        assert {r["l0_node_id"] for r in data} == extracted_ids
        assert all(r["trust_state"] == "auto-verified" for r in data)
        assert all(r["check_failures"] == [] for r in data)

        con = sqlite3.connect(store["db"])
        depths = con.execute(
            "SELECT depth FROM node WHERE kind = 'summary' AND tier = 'notes'"
        ).fetchall()
        prov_edges = con.execute(
            "SELECT COUNT(*) FROM edge WHERE type = 'provenance' "
            "AND relation = 'derived_from' AND from_node IN "
            "(SELECT id FROM node WHERE kind = 'summary')"
        ).fetchone()[0]
        con.close()
        assert sorted(r[0] for r in depths) == [2, 2, 2]
        assert prov_edges == 3

    def test_derive_all_mixed_extracted_and_legacy(self, store):
        """derive --all processes extracted roots AND legacy raw_source L0s."""
        pairs = self._register_n(store, 2)
        raw = self._ingest_n(store, 2, prefix="legacy")
        expected = {p["id"] for p in pairs} | {r["id"] for r in raw}

        result = self._derive_all(store, limit=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 4
        assert {r["l0_node_id"] for r in data} == expected
        assert all(r["status"] == "derived" for r in data)

    def test_derive_all_capped_by_limit(self, store):
        """5 L0s, --limit 3 -> only 3 derivations created."""
        self._ingest_n(store, 5)
        result = self._derive_all(store, limit=3)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        # 3 derived, 2 not reached (limit caps before processing them)
        assert len(data) == 3
        assert all(r["status"] == "derived" for r in data)

        con = sqlite3.connect(store["db"])
        count = con.execute(
            "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
        ).fetchone()[0]
        con.close()
        assert count == 3, f"expected 3 derivations, got {count}"

    def test_derive_all_skips_already_derived(self, store):
        """5 L0s, derive 2 manually, then --all -> 3 new derivations + 2 already_derived."""
        l0s = self._ingest_n(store, 5)
        # Derive first 2 manually
        for l0 in l0s[:2]:
            d = _run_memex(
                ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), l0["id"]],
                env={"MEMEX_AGENT": FAKE_AGENT},
            )
            assert d.returncode == 0, d.stderr

        # Now --all with limit 10: 2 already_derived + 3 new
        result = self._derive_all(store, limit=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 5
        already = [r for r in data if r["status"] == "already_derived"]
        derived = [r for r in data if r["status"] == "derived"]
        assert len(already) == 2
        assert len(derived) == 3

        con = sqlite3.connect(store["db"])
        count = con.execute(
            "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
        ).fetchone()[0]
        con.close()
        assert count == 5

    def test_derive_all_no_un_derived(self, store):
        """All L0s already derived -> all reported as already_derived."""
        l0s = self._ingest_n(store, 3)
        for l0 in l0s:
            d = _run_memex(
                ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), l0["id"]],
                env={"MEMEX_AGENT": FAKE_AGENT},
            )
            assert d.returncode == 0, d.stderr

        result = self._derive_all(store, limit=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 3
        assert all(r["status"] == "already_derived" for r in data)

    def test_derive_all_output_format(self, store):
        """Validate JSON output structure — includes already_derived entries too."""
        l0s = self._ingest_n(store, 2)
        # Derive one manually so we see already_derived too
        _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), l0s[0]["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )

        result = self._derive_all(store, limit=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 2  # 1 already_derived + 1 derived

        already = [r for r in data if r["status"] == "already_derived"]
        derived = [r for r in data if r["status"] == "derived"]
        assert len(already) == 1
        assert len(derived) == 1

        entry = derived[0]
        assert "id" in entry
        assert "l0_node_id" in entry
        assert "trust_state" in entry
        assert "check_failures" in entry

    def test_derive_all_without_limit_processes_everything(self, store):
        """--all without --limit derives ALL un-derived nodes (no 10-node cap)."""
        self._ingest_n(store, 12)
        result = self._derive_all(store)  # no --limit flag at all
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 12
        assert all(r["status"] == "derived" for r in data)

        con = sqlite3.connect(store["db"])
        count = con.execute(
            "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
        ).fetchone()[0]
        con.close()
        assert count == 12, f"expected 12 derivations, got {count}"

    def test_derive_all_limit_zero_processes_more_than_ten(self, store):
        """--limit 0 on >10 un-derived nodes derives all of them."""
        self._ingest_n(store, 11)
        result = self._derive_all(store, limit=0)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 11
        assert all(r["status"] == "derived" for r in data)

    def test_derive_all_limit_negative_is_unlimited(self, store):
        """--limit -1 on >10 un-derived nodes also derives all of them (<= 0 = unlimited)."""
        self._ingest_n(store, 11)
        result = self._derive_all(store, limit=-1)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 11
        assert all(r["status"] == "derived" for r in data)

    def test_derive_all_handles_errors(self, store):
        """Failing agent returns error status without crashing batch."""
        self._ingest_n(store, 3)
        result = self._derive_all(store, limit=10, agent=FAKE_THROWS_AGENT)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 3
        for entry in data:
            assert entry["status"] == "error"
            assert "detail" in entry
            assert "Simulated LLM failure" in entry["detail"]

    def test_derive_all_idempotent(self, store):
        """Re-run with same state -> all reported as already_derived."""
        self._ingest_n(store, 3)
        result = self._derive_all(store, limit=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 3
        assert all(r["status"] == "derived" for r in data)

        # Re-run — all now already_derived
        result = self._derive_all(store, limit=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) == 3
        assert all(r["status"] == "already_derived" for r in data)

    def test_derive_all_no_l0s(self, store):
        """No L0s at all -> empty result."""
        result = self._derive_all(store, limit=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == []

    def test_single_derive_unchanged(self, store):
        """Original derive <node-id> still works unchanged."""
        l0s = self._ingest_n(store, 1)
        result = _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), l0s[0]["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "derived"
        assert data["l0_node_id"] == l0s[0]["id"]


class TestDeriveNoContentGate:
    """Ticket #141: L0 content missing or below MIN_CHARS is skipped — the
    derivation returns status no_content, no summary node, no agent call."""

    def test_derive_short_l0_returns_no_content(self, store):
        ingested = _seed_raw_source_short(store, "short.md", "https://example.com/short")
        result = _derive(store, ingested["id"])

        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "no_content"
        assert data["l0_node_id"] == ingested["id"]

        con = sqlite3.connect(store["db"])
        try:
            summary_count = con.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'summary'"
            ).fetchone()[0]
        finally:
            con.close()
        assert summary_count == 0

    def test_derive_short_l0_does_not_call_agent(self, store):
        """The agent must never be invoked for a content-less L0."""
        from tests.fake_llm_client import FakeAgent

        class RecordingFake(FakeAgent):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def derive(self, content):
                self.calls += 1
                return super().derive(content)

        ingested = _seed_raw_source_short(store, "short.md", "https://example.com/short")
        agent = RecordingFake()
        with _Store.open(store["db"]) as s:
            from memex.services.derive import DeriverService

            svc = DeriverService(s, Path(store["vault"]), agent)
            result = svc.derive(ingested["id"])

        assert result.status == "no_content"
        assert agent.calls == 0

        con = sqlite3.connect(store["db"])
        try:
            summary_count = con.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'summary'"
            ).fetchone()[0]
        finally:
            con.close()
        assert summary_count == 0

    def test_reader_agent_short_non_ascii_l0_returns_no_content(self, store):
        """Reader agents get a DocumentRef, so the MIN_CHARS gate must count
        characters, not file bytes: a 60-char non-ASCII L0 (~120 UTF-8 bytes)
        is below the floor and must be skipped (ticket #141)."""
        from tests.fake_llm_client import FakeReaderAgent

        class RecordingReader(FakeReaderAgent):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def derive(self, **kwargs):
                self.calls += 1
                return super().derive(**kwargs)

        node_id = str(uuid.uuid4())
        md_path = Path(store["vault"]) / "non-ascii-short.md"
        md_path.write_text("à" * 60, encoding="utf-8")
        assert len(md_path.read_text(encoding="utf-8")) == 60
        assert md_path.stat().st_size > 100

        con = sqlite3.connect(store["db"])
        st = _Store(con)
        st.create_node(
            node_id=node_id, kind="raw_source", depth=0,
            content_path=str(md_path), created_at=datetime.now(timezone.utc).isoformat(),
        )
        st.attach_source(
            node_id=node_id, canonical_key="https://example.com/non-ascii-short",
            source_url="https://example.com/non-ascii-short", title="Short",
            fetched_at=None,
        )
        con.commit()
        con.close()

        agent = RecordingReader()
        with _Store.open(store["db"]) as s:
            from memex.services.derive import DeriverService

            svc = DeriverService(s, Path(store["vault"]), agent)
            result = svc.derive(node_id)

        assert result.status == "no_content"
        assert agent.calls == 0

        con = sqlite3.connect(store["db"])
        try:
            summary_count = con.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'summary'"
            ).fetchone()[0]
        finally:
            con.close()
        assert summary_count == 0

    def test_derive_short_l0_does_not_block_real_derive(self, store):
        """A real-content L0 next to a short one derives unchanged."""
        short = _seed_raw_source_short(store, "short.md", "https://example.com/short")
        long = _seed_raw_source(store, "long.md", "https://example.com/long")
        result = _derive(store, long["id"])

        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "derived"
        assert data["trust_state"] == "auto-verified"
        assert data["l0_node_id"] == long["id"]

        # The short L0 was never touched (no derivation on it).
        con = sqlite3.connect(store["db"])
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM edge WHERE to_node = ? "
                "AND type = 'provenance' AND relation = 'derived_from'",
                (short["id"],),
            ).fetchone()[0]
        finally:
            con.close()
        assert count == 0


class TestDeriveAllNoContent:
    """derive --all reports no_content for content-less L0s instead of
    deriving process-description notes (ticket #141)."""

    def _derive_all(self, store, limit: int | None = None):
        args = [
            "derive", "--db", str(store["db"]),
            "--vault", str(store["vault"]), "--all",
        ]
        if limit is not None:
            args.extend(["--limit", str(limit)])
        return _run_memex(args, env={"MEMEX_AGENT": FAKE_AGENT})

    def test_derive_all_reports_no_content_for_short_l0s(self, store):
        short = _seed_raw_source_short(store, "short.md", "https://example.com/short")
        long = _seed_raw_source(store, "long.md", "https://example.com/long")

        result = self._derive_all(store)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)

        statuses = {r["l0_node_id"]: r["status"] for r in data}
        assert statuses[short["id"]] == "no_content"
        assert statuses[long["id"]] == "derived"

        con = sqlite3.connect(store["db"])
        try:
            summary_count = con.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'summary'"
            ).fetchone()[0]
        finally:
            con.close()
        assert summary_count == 1  # only the real-content L0 got a summary


class TestSearch:
    def _search(self, store, query: str):
        return _run_memex(
            ["search", "--db", str(store["db"]), "--vault", str(store["vault"]), query],
        )

    def test_search_returns_json_array(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        _derive(store, ingested["id"])
        result = self._search(store, "Synthesis")
        data = json.loads(result.stdout)
        assert isinstance(data, list)

    def test_search_matches_derivation_content(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        _derive(store, ingested["id"])
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert len(data) >= 1

    def test_search_result_has_required_fields(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        l0_id = ingested["id"]
        _derive(store, l0_id)
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert len(data) >= 1
        item = data[0]
        assert "id" in item
        assert "snippet" in item
        assert "canonical_key" in item
        assert "l0_node_id" in item

    def test_search_snippet_contains_query(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        _derive(store, ingested["id"])
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert "broader pattern" in data[0]["snippet"].lower()

    def test_search_returns_empty_array_for_no_match(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        _derive(store, ingested["id"])
        result = self._search(store, "xyznonexistentterm")
        assert json.loads(result.stdout) == []

    def test_search_is_readonly(self, store):
        import sqlite3
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        _derive(store, ingested["id"])

        con = sqlite3.connect(store["db"])
        n_before = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        e_before = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()

        self._search(store, "broader pattern")

        con = sqlite3.connect(store["db"])
        n_after = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
        e_after = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        con.close()
        assert n_before == n_after
        assert e_before == e_after

    def test_search_l0_node_id_points_to_l0(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        l0_id = ingested["id"]
        _derive(store, l0_id)
        result = self._search(store, "broader pattern")
        data = json.loads(result.stdout)
        assert data[0]["l0_node_id"] == l0_id


class TestDeriveQualityGate:
    """Integration tests for the adversarial validation gate in _do_derive.

    Validator is injected via MEMEX_VALIDATOR env var.
    """

    FAKE_VALIDATOR_FAILS = "tests.fake_validator_fails:FakeValidatorFails"
    FAKE_VALIDATOR_WARNS = "tests.fake_validator_warns:FakeValidatorWarns"

    @staticmethod
    def _ingest(store, url: str) -> dict:
        """Register a test file and return the ingested node dict."""
        import uuid
        filename = f"{uuid.uuid4().hex}.md"
        vault = Path(store["vault"])
        p = register_node(store, vault, filename, url)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)

    def test_no_validator_proceeds(self, store):
        """No MEMEX_VALIDATOR set -> derive proceeds normally (no regression)."""
        ingested = self._ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "derived"

    def test_fake_agent_validator_skips(self, store):
        """MEMEX_VALIDATOR=FakeAgent (no call_llm) -> validation skipped, derive proceeds."""
        ingested = self._ingest(store, "https://example.com/article")
        result = _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), ingested["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT, "MEMEX_VALIDATOR": FAKE_AGENT},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "derived"

    def test_failing_validator_rejects(self, store):
        """Validator rejects -> quality_failed, no node or edge created."""
        ingested = self._ingest(store, "https://example.com/article")
        result = _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), ingested["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT, "MEMEX_VALIDATOR": self.FAKE_VALIDATOR_FAILS},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "quality_failed"
        assert "Derivation does not meaningfully re-elaborate" in data["reason"]
        assert data["l0_node_id"] == ingested["id"]
        # Verify no notes-tier summary node was created
        conn = sqlite3.connect(store["db"])
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
            ).fetchone()[0]
            assert count == 0, f"Expected 0 summary nodes, got {count}"
        finally:
            conn.close()

    def test_warning_validator_proceeds_with_warning(self, store):
        """Validator warns -> derive proceeds but warning on stderr."""
        ingested = self._ingest(store, "https://example.com/article")
        result = _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), ingested["id"]],
            env={"MEMEX_AGENT": FAKE_AGENT, "MEMEX_VALIDATOR": self.FAKE_VALIDATOR_WARNS},
        )
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "derived"
        # Warning should be on stderr
        warning = json.loads(result.stderr.strip())
        assert "validator_warning" in warning
        assert "Validator LLM call failed" in warning["validator_warning"]


class TestParseDeriveResponse:
    """parse_derive_response — JSON envelope, fences, and regex fallback."""

    def test_parses_bare_json_envelope(self):
        """Bare JSON envelope yields prose and statements."""
        from memex.utils.parsing import parse_derive_response

        raw = json.dumps(
            {
                "prose": "# Title\n\nBody.",
                "synthesis_statements": ["A statement."],
            }
        )
        prose, statements = parse_derive_response(raw)
        assert prose == "# Title\n\nBody."
        assert statements == ["A statement."]

    def test_parses_fenced_json_envelope(self):
        """Markdown-fenced JSON envelope (CLI agents) parses the same way."""
        from memex.utils.parsing import parse_derive_response

        raw = (
            "```json\n"
            + json.dumps(
                {
                    "prose": "# Title\n\nBody.",
                    "synthesis_statements": ["A statement."],
                }
            )
            + "\n```"
        )
        prose, statements = parse_derive_response(raw)
        assert prose == "# Title\n\nBody."
        assert statements == ["A statement."]

    def test_fallback_recovers_synthesis_markers(self):
        """Non-JSON prose falls back to regex recovery of > Synthesis: markers (Rule S5)."""
        from memex.utils.parsing import parse_derive_response

        raw = "# Title\n\nBody prose.\n\n> Synthesis: An inference.\n> Synthesis: Another one."
        prose, statements = parse_derive_response(raw)
        assert prose == raw
        assert statements == ["An inference.", "Another one."]

    def test_fallback_empty_statements_without_markers(self):
        """Prose without markers yields an empty statement list."""
        from memex.utils.parsing import parse_derive_response

        prose, statements = parse_derive_response("# Title\n\nNo markers here.")
        assert prose == "# Title\n\nNo markers here."
        assert statements == []


class TestPromptCap:
    """_cap_prompt_content — NUL strip + size cap for the LLM prompt."""

    def test_nul_bytes_stripped(self):
        from memex.services.derive import _cap_prompt_content

        out = _cap_prompt_content("a\x00b\x00c")
        assert out == "abc"

    def test_short_content_untouched(self):
        from memex.services.derive import _cap_prompt_content

        out = _cap_prompt_content("short content")
        assert out == "short content"

    def test_long_content_capped_with_marker(self):
        from memex.services.derive import _cap_prompt_content

        out = _cap_prompt_content("x" * 300_000)
        assert len(out) < 300_000
        assert "source content truncated" in out
        assert out.startswith("x" * 120_000)


class TestReaderAgentMode:
    """Reader-capable agents receive a DocumentRef, never inlined content."""

    def test_derive_passes_reference_to_reader_agent(self, store):
        from tests.fake_llm_client import FakeReaderAgent

        vault = Path(store["vault"])
        p = register_node(store, vault, "reader.md", "https://example.com/reader")
        ingested = json.loads(p.stdout)
        agent = FakeReaderAgent()
        with _Store.open(store["db"]) as s:
            from memex.services.derive import DeriverService

            svc = DeriverService(s, vault, agent)
            result = svc.derive(ingested["id"])

        assert result.status == "derived"
        assert agent.received["content"] is None
        ref = agent.received["reference"]
        assert ref is not None
        assert ref.node_id == ingested["id"]
        assert Path(ref.content_path).exists()
        assert ref.size_bytes > 0

    def test_derive_passes_content_to_non_reader(self, store):
        from tests.fake_llm_client import FakeAgent

        class RecordingFake(FakeAgent):
            def __init__(self):
                super().__init__()
                self.received = {}

            def derive(self, content):
                self.received = {"content": content}
                return super().derive(content)

        vault = Path(store["vault"])
        p = register_node(store, vault, "plain.md", "https://example.com/plain")
        ingested = json.loads(p.stdout)
        agent = RecordingFake()
        with _Store.open(store["db"]) as s:
            from memex.services.derive import DeriverService

            svc = DeriverService(s, vault, agent)
            result = svc.derive(ingested["id"])

        assert result.status == "derived"
        # Non-reader agents still get the inlined content.
        assert "Test Article" in agent.received["content"]
        assert agent.received["content"] is not None


class TestCanonicalizeSynthesisMarkers:
    """Pure helper ``canonicalize_synthesis_markers`` (ticket #143).

    The file's ``> Synthesis:`` markers are the presentation channel; the
    ``synthesis_statements`` column is the source of truth the D3 check
    compares the file against. The helper rewrites the file's markers from
    the column so D3 always passes for valid derivations.
    """

    def test_replaces_markers_in_order_with_statements(self):
        from memex.services.derive import canonicalize_synthesis_markers

        prose = (
            "# Title\n\nBody.\n\n"
            "> Synthesis: The 'x' claim\n"
            "> Synthesis: Another 'y' claim\n"
        )
        out = canonicalize_synthesis_markers(
            prose, ['The "x" claim', 'Another "y" claim']
        )
        assert out == (
            "# Title\n\nBody.\n\n"
            '> Synthesis: The "x" claim\n'
            '> Synthesis: Another "y" claim\n'
        )

    def test_drops_extra_markers_beyond_statements(self):
        from memex.services.derive import canonicalize_synthesis_markers

        prose = (
            "# Title\n\nBody.\n\n"
            "> Synthesis: one\n"
            "> Synthesis: two\n"
            "> Synthesis: three\n"
            "> Synthesis: four\n"
            "> Synthesis: five\n"
        )
        out = canonicalize_synthesis_markers(
            prose, ["one", "two", "three", "four"]
        )
        assert out.count("> Synthesis:") == 4
        assert out == (
            "# Title\n\nBody.\n\n"
            "> Synthesis: one\n"
            "> Synthesis: two\n"
            "> Synthesis: three\n"
            "> Synthesis: four\n"
        )

    def test_appends_synthesis_section_when_prose_has_no_markers(self):
        from memex.services.derive import canonicalize_synthesis_markers

        prose = "# Title\n\nBody without any markers."
        out = canonicalize_synthesis_markers(
            prose, ["Statement one", "Statement two"]
        )
        assert out == (
            "# Title\n\nBody without any markers.\n\n"
            "## Synthesis\n"
            "> Synthesis: Statement one\n"
            "> Synthesis: Statement two"
        )

    def test_fewer_markers_than_statements_appends_remaining(self):
        """Fewer prose markers than statements → the remaining statements are
        appended (in a ``## Synthesis`` section) so the file ends up with
        EXACTLY len(statements) markers (canonical contract)."""
        from memex.services.derive import canonicalize_synthesis_markers

        prose = (
            "# Title\n\nBody.\n\n"
            "> Synthesis: one\n"
        )
        out = canonicalize_synthesis_markers(prose, ["one", "two", "three"])
        assert out == (
            "# Title\n\nBody.\n\n"
            "> Synthesis: one\n"
            "\n"
            "## Synthesis\n"
            "> Synthesis: two\n"
            "> Synthesis: three\n"
        )

    def test_leaves_prose_untouched_without_statements(self):
        from memex.services.derive import canonicalize_synthesis_markers

        prose = "# Title\n\nBody without any markers."
        assert canonicalize_synthesis_markers(prose, []) == prose

    def test_keeps_non_marker_lines_intact(self):
        from memex.services.derive import canonicalize_synthesis_markers

        prose = (
            "# Title\n\n"
            "Body prose mentioning > Synthesis: not a marker mid-line.\n\n"
            "> Synthesis: old\n"
        )
        out = canonicalize_synthesis_markers(prose, ["new statement"])
        assert "Body prose mentioning > Synthesis: not a marker mid-line." in out
        assert out.endswith("> Synthesis: new statement\n")


class TestDeriveCanonicalizeMarkers:
    """Ticket #143 — derive canonicalizes the file's > Synthesis: markers
    from the synthesis_statements column so D3 passes (auto-verified).

    Exercises the full service path in-process: agent call → file write →
    column store → deterministic checks → trust state.
    """

    @staticmethod
    def _ingest(store, url: str) -> str:
        import uuid

        filename = f"{uuid.uuid4().hex}.md"
        vault = Path(store["vault"])
        p = register_node(store, vault, filename, url)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)["id"]

    @staticmethod
    def _derive(store, node_id: str, agent):
        from memex.services.derive import DeriverService

        with _Store.open(store["db"]) as s:
            return DeriverService(s, Path(store["vault"]), agent).derive(node_id)

    @staticmethod
    def _file_markers(store, content_path: str) -> list[str]:
        import re

        content = Path(content_path).read_text(encoding="utf-8")
        return re.findall(r"> Synthesis:\s*(.*)", content)

    @staticmethod
    def _db_statements(store, deriv_id: str) -> list[str]:
        con = sqlite3.connect(store["db"])
        try:
            row = con.execute(
                "SELECT synthesis_statements FROM node WHERE id = ?",
                (deriv_id,),
            ).fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row and row[0] else []

    def test_divergent_quote_style_markers_are_canonicalized(self, store):
        """Prose markers with different quoting than the column → written file
        markers equal the column exactly → D3 passes → auto-verified (was draft)."""
        from tests.fake_llm_client import FakeAgentDivergent

        node_id = self._ingest(store, "https://example.com/article")
        agent = FakeAgentDivergent(
            prose=(
                "# The Claim\n\n"
                "This article discusses the topic at hand and its broader implications.\n\n"
                "> Synthesis: The 'x' claim\n\n"
                "The source material covers the subject thoroughly."
            ),
            statements=['The "x" claim'],
        )
        result = self._derive(store, node_id, agent)

        assert result.status == "derived"
        assert result.trust_state == "auto-verified"
        assert result.check_failures == []
        assert self._file_markers(store, result.content_path) == ['The "x" claim']
        assert self._file_markers(store, result.content_path) == self._db_statements(
            store, result.id
        )

    def test_extra_file_markers_are_dropped_to_statement_count(self, store):
        """5 prose markers but 4 statements → the file gets exactly 4 markers
        (the extra agent marker is dropped) → D3 count check passes."""
        from tests.fake_llm_client import FakeAgentDivergent

        node_id = self._ingest(store, "https://example.com/article")
        agent = FakeAgentDivergent(
            prose=(
                "# The Claim\n\n"
                "This article discusses the topic at hand and its broader implications.\n\n"
                "> Synthesis: one\n"
                "> Synthesis: two\n"
                "> Synthesis: three\n"
                "> Synthesis: four\n"
                "> Synthesis: five\n\n"
                "The source material covers the subject thoroughly."
            ),
            statements=["one", "two", "three", "four"],
        )
        result = self._derive(store, node_id, agent)

        assert result.status == "derived"
        assert result.trust_state == "auto-verified"
        assert result.check_failures == []
        markers = self._file_markers(store, result.content_path)
        assert markers == ["one", "two", "three", "four"]
        assert len(markers) == len(self._db_statements(store, result.id))

    def test_no_markers_but_statements_appends_synthesis_section(self, store):
        """Prose without markers but statements present → the file gains a
        ``## Synthesis`` section with one marker per statement → D3 passes."""
        from tests.fake_llm_client import FakeAgentDivergent

        node_id = self._ingest(store, "https://example.com/article")
        agent = FakeAgentDivergent(
            prose=(
                "# The Claim\n\n"
                "This article discusses the topic at hand and its broader implications.\n\n"
                "The source material covers the subject thoroughly."
            ),
            statements=[
                "A canonical statement",
                "Another canonical statement",
            ],
        )
        result = self._derive(store, node_id, agent)

        assert result.status == "derived"
        assert result.trust_state == "auto-verified"
        assert result.check_failures == []
        content = Path(result.content_path).read_text(encoding="utf-8")
        assert "## Synthesis" in content
        assert self._file_markers(store, result.content_path) == [
            "A canonical statement",
            "Another canonical statement",
        ]

    def test_no_statements_leaves_prose_unchanged_and_still_draft(self, store):
        """No statements and no markers → prose untouched; D3 still fails, so
        the derivation stays draft (unchanged behavior)."""
        from tests.fake_llm_client import FakeAgentDivergent

        node_id = self._ingest(store, "https://example.com/article")
        prose = (
            "# The Claim\n\n"
            "This article discusses the topic at hand and its broader implications.\n\n"
            "The source material covers the subject thoroughly."
        )
        agent = FakeAgentDivergent(prose=prose, statements=[])
        result = self._derive(store, node_id, agent)

        assert result.status == "derived"
        assert result.trust_state == "draft"
        assert any(
            "Synthesis marker check failed" in f for f in result.check_failures
        )
        assert Path(result.content_path).read_text(encoding="utf-8") == prose
