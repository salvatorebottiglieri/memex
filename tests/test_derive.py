"""Tests for `memex derive <node-id>` and `memex search <query>`.

Agent is injected via MEMEX_AGENT — no real Anthropic calls.
The fake agent module lives at tests/fake_llm_client.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import sqlite3

from tests.conftest import _run_memex, register_node, WORKTREE


FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_FAILING_AGENT = "tests.fake_llm_client_failing:FakeLLMClientFailing"
FAKE_THROWS_AGENT = "tests.fake_llm_client_throws:FakeLLMClientThrows"



def _derive(store, node_id: str) -> "subprocess.CompletedProcess":  # type: ignore[name-defined]
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )


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
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
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

    def test_derive_inserts_provenance_edge(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        l0_id = ingested["id"]
        result = _derive(store, l0_id)
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT type, relation, from_node, to_node FROM edge "
            "WHERE from_node = ? AND to_node = ?",
            (deriv_id, l0_id),
        ).fetchone()
        con.close()

        assert row is not None
        assert row[0] == "provenance"
        assert row[1] == "derived_from"
        assert row[2] == deriv_id
        assert row[3] == l0_id

    def test_derive_writes_markdown_file_with_synthesis_markers(self, store):
        vault = Path(store["vault"])
        p = register_node(store, vault, "test.md", "https://example.com/article")
        ingested = json.loads(p.stdout)
        result = _derive(store, ingested["id"])
        data = json.loads(result.stdout)
        md_path = Path(data.get("content_path", str(store["vault"] / f"{data['id']}.md")))

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
        """Register n unique nodes and return their result dicts."""
        results = []
        vault = Path(store["vault"])
        for i in range(n):
            p = register_node(store, vault, f"{prefix}-{i}.md", f"https://example.com/{prefix}-{i}")
            assert p.returncode == 0, p.stderr
            results.append(json.loads(p.stdout))
        return results

    def test_derive_all_capped_by_limit(self, store):
        """5 L0s, --limit 3 -> only 3 derivations created."""
        l0s = self._ingest_n(store, 5)
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

    def test_derive_all_limit_zero(self, store):
        """limit=0 -> empty result."""
        self._ingest_n(store, 3)
        result = self._derive_all(store, limit=0)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data == []

    def test_derive_all_handles_errors(self, store):
        """Failing agent returns error status without crashing batch."""
        l0s = self._ingest_n(store, 3)
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
        l0s = self._ingest_n(store, 3)
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
