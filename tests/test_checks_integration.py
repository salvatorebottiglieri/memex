"""Integration tests: checks module wired into derive command and surfaced in show/list.

Tests exercise the CLI seam — no mocking of internals.
Fixture derivations cover both passing and failing cases.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


FAKE_FETCHER = "tests.fake_fetcher:FakeFetcher"
FAKE_LLM = "tests.fake_llm_client:FakeLLMClient"
FAKE_FAILING_LLM = "tests.fake_llm_client_failing:FakeLLMClientFailing"
WORKTREE = Path("/home/sbottiglieri/memex-issue-6")


def run_memex(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "memex.cli"] + args,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=full_env,
    )


@pytest.fixture()
def store(tmp_path):
    """Initialised db + vault."""
    db_path = tmp_path / "memex.db"
    vault_path = tmp_path / "vault"
    run_memex(
        ["init", "--db", str(db_path), "--vault", str(vault_path)],
        cwd=WORKTREE,
    )
    return {"db": db_path, "vault": vault_path, "tmp": tmp_path}


def ingest(store, url: str) -> dict:
    env = {"MEMEX_FETCHER_MODULE": FAKE_FETCHER}
    result = run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        cwd=WORKTREE,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def derive(store, node_id: str, llm_module: str = FAKE_LLM) -> subprocess.CompletedProcess:
    env = {"MEMEX_LLM_MODULE": llm_module}
    return run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        cwd=WORKTREE,
        env=env,
    )


def show(store, node_id: str) -> subprocess.CompletedProcess:
    return run_memex(
        ["show", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        cwd=WORKTREE,
    )


def list_nodes(store) -> subprocess.CompletedProcess:
    return run_memex(
        ["list", "--db", str(store["db"]), "--vault", str(store["vault"])],
        cwd=WORKTREE,
    )


# ---------------------------------------------------------------------------
# Acceptance criterion: passing derivation → auto-verified
# ---------------------------------------------------------------------------

class TestAutoVerifiedOnPassingDerivation:
    def test_passing_derivation_gets_auto_verified_trust_state(self, store):
        """A derivation that passes all checks is stored with trust_state=auto-verified."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        result = derive(store, l0_id)
        assert result.returncode == 0, result.stderr
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT trust_state FROM node WHERE id = ?", (deriv_id,)
        ).fetchone()
        con.close()

        assert row is not None
        assert row[0] == "auto-verified"

    def test_derive_response_reflects_auto_verified(self, store):
        """The derive JSON response includes the resolved trust_state."""
        ingested = ingest(store, "https://example.com/article")
        result = derive(store, ingested["id"])
        data = json.loads(result.stdout)

        assert data["trust_state"] == "auto-verified"

    def test_auto_verified_node_has_no_check_failures(self, store):
        """An auto-verified node's show output has check_failures=[]."""
        ingested = ingest(store, "https://example.com/article")
        result = derive(store, ingested["id"])
        deriv_id = json.loads(result.stdout)["id"]

        show_result = show(store, deriv_id)
        data = json.loads(show_result.stdout)

        assert data.get("check_failures") == []


# ---------------------------------------------------------------------------
# Acceptance criterion: failing derivation → stays draft + failures recorded
# ---------------------------------------------------------------------------

class TestDraftOnFailingDerivation:
    def test_no_synthesis_marker_stays_draft(self, store):
        """A derivation missing "> Synthesis:" stays draft and is flagged."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]
        result = derive(store, l0_id, llm_module=FAKE_FAILING_LLM)
        assert result.returncode == 0, result.stderr
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT trust_state FROM node WHERE id = ?", (deriv_id,)
        ).fetchone()
        con.close()

        assert row is not None
        assert row[0] == "draft"

    def test_failing_derive_response_reflects_draft_trust_state(self, store):
        """The derive JSON response for a failing check includes trust_state=draft."""
        ingested = ingest(store, "https://example.com/article")
        result = derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)
        data = json.loads(result.stdout)

        assert data["trust_state"] == "draft"

    def test_failing_derive_response_includes_check_failures(self, store):
        """The derive JSON response for a failing check includes check_failures list."""
        ingested = ingest(store, "https://example.com/article")
        result = derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)
        data = json.loads(result.stdout)

        assert "check_failures" in data
        assert isinstance(data["check_failures"], list)
        assert len(data["check_failures"]) > 0


# ---------------------------------------------------------------------------
# Acceptance criterion: show surfaces check_failures for draft nodes
# ---------------------------------------------------------------------------

class TestShowSurfacesCheckFailures:
    def test_show_draft_node_includes_check_failures_field(self, store):
        """memex show <id> includes check_failures for draft derivation nodes."""
        ingested = ingest(store, "https://example.com/article")
        derive_result = derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)
        deriv_id = json.loads(derive_result.stdout)["id"]

        show_result = show(store, deriv_id)
        assert show_result.returncode == 0, show_result.stderr
        data = json.loads(show_result.stdout)

        assert "check_failures" in data
        assert isinstance(data["check_failures"], list)
        assert len(data["check_failures"]) > 0

    def test_show_check_failures_contains_synthesis_failure_message(self, store):
        """check_failures messages mention the specific checks that failed."""
        ingested = ingest(store, "https://example.com/article")
        derive_result = derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)
        deriv_id = json.loads(derive_result.stdout)["id"]

        show_result = show(store, deriv_id)
        data = json.loads(show_result.stdout)

        # The fake failing LLM omits the synthesis marker — we should see that in failures
        failures_text = " ".join(data["check_failures"]).lower()
        assert "synthesis" in failures_text

    def test_show_l0_node_has_no_check_failures_field(self, store):
        """An L0 (raw_source) node has check_failures=None (checks don't apply to L0)."""
        ingested = ingest(store, "https://example.com/article")
        l0_id = ingested["id"]

        show_result = show(store, l0_id)
        data = json.loads(show_result.stdout)

        # L0 nodes are not derivations; check_failures should be None or absent
        assert data.get("check_failures") is None


# ---------------------------------------------------------------------------
# Acceptance criterion: list distinguishes draft from auto-verified
# ---------------------------------------------------------------------------

class TestListTrustState:
    def test_list_includes_trust_state_field(self, store):
        """memex list includes trust_state for each node."""
        ingest(store, "https://example.com/article")
        result = list_nodes(store)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert len(data) >= 1
        assert "trust_state" in data[0]

    def test_list_shows_auto_verified_after_passing_derive(self, store):
        """After a passing derive, list shows the derivation with trust_state=auto-verified."""
        ingested = ingest(store, "https://example.com/article")
        derive(store, ingested["id"])

        result = list_nodes(store)
        data = json.loads(result.stdout)

        trust_states = [n["trust_state"] for n in data]
        assert "auto-verified" in trust_states

    def test_list_shows_draft_after_failing_derive(self, store):
        """After a failing derive, list shows the derivation with trust_state=draft."""
        ingested = ingest(store, "https://example.com/article")
        derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)

        result = list_nodes(store)
        data = json.loads(result.stdout)

        # The derivation node should be draft
        deriv_nodes = [n for n in data if n["kind"] != "raw_source"]
        assert len(deriv_nodes) >= 1
        assert all(n["trust_state"] == "draft" for n in deriv_nodes)
