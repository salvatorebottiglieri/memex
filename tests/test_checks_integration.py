"""Integration tests: checks module wired into the derive command and surfaced in show/list.

Tests exercise the CLI seam — no mocking of internals.
"""
from __future__ import annotations

import json
import sqlite3

from tests.conftest import _run_memex, FAKE_FETCHER


FAKE_LLM = "tests.fake_llm_client:FakeLLMClient"
FAKE_FAILING_LLM = "tests.fake_llm_client_failing:FakeLLMClientFailing"


def _ingest(store, url: str) -> dict:
    result = _run_memex(
        ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _derive(store, node_id: str, llm_module: str = FAKE_LLM):
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_LLM_MODULE": llm_module},
    )


def _show(store, node_id: str):
    return _run_memex(
        ["show", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
    )


class TestPassingDerivation:
    def test_passing_derivation_is_auto_verified(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["trust_state"] == "auto-verified"
        assert data["check_failures"] == []

    def test_passing_derivation_shows_no_failures(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        deriv_id = json.loads(result.stdout)["id"]
        show = _show(store, deriv_id)
        data = json.loads(show.stdout)
        assert data["trust_state"] == "auto-verified"
        assert data["check_failures"] == []


class TestFailingDerivation:
    def test_failing_derivation_stays_draft(self, store):
        """FakeLLMClientFailing produces a derivation without > Synthesis: marker
        and shorter than MIN_CHARS, so the node stays in draft."""
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)
        data = json.loads(result.stdout)
        assert data["trust_state"] == "draft"
        assert len(data["check_failures"]) >= 1

    def test_failing_derivation_failures_are_persisted(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)
        deriv_id = json.loads(result.stdout)["id"]

        show = _show(store, deriv_id)
        data = json.loads(show.stdout)
        assert data["trust_state"] == "draft"
        assert isinstance(data["check_failures"], list)
        assert len(data["check_failures"]) >= 1

    def test_failing_derivation_failures_in_db(self, store):
        """The check_failures JSON column on node is populated for draft derivations."""
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"], llm_module=FAKE_FAILING_LLM)
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT trust_state, check_failures FROM node WHERE id = ?", (deriv_id,)
        ).fetchone()
        con.close()
        assert row[0] == "draft"
        assert row[1] is not None  # JSON string, not NULL
        failures = json.loads(row[1])
        assert len(failures) >= 1


class TestListShowsDerivation:
    def test_list_includes_summary_nodes(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])
        result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        data = json.loads(result.stdout)
        kinds = {row["kind"] for row in data}
        assert "raw_source" in kinds
        assert "summary" in kinds