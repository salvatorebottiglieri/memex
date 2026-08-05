"""Core invariants — trust-state machine (ADR-0004, ADR-0014).

    T1  schema CHECK constraint: only draft/auto-verified/human-approved/stale
    T2  derive with passing checks -> auto-verified, empty check_failures
    T3  derive with failing checks -> draft + check_failures recorded
    T4  agent failure -> status=error, no summary node created, no partial state
    T5  derive is idempotent: second call -> already_derived, still exactly
        one summary node per L0
    T6  draft -> auto-verified only through the checks gate (run_checks passed)

The state machine contract: a node enters draft; it may only reach
auto-verified when every deterministic check passes; any check failure
leaves it draft and records the failures.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from memex.store import StoreError
from tests.conftest import _run_memex, register_node

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_FAILING_AGENT = "tests.fake_llm_client_failing:FakeLLMClientFailing"
FAKE_THROWS_AGENT = "tests.fake_llm_client_throws:FakeLLMClientThrows"


def _q(store, sql: str, params: tuple = ()) -> list:
    con = sqlite3.connect(str(store["db"]))
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _derive(store, node_id: str, agent: str = FAKE_AGENT):
    result = _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": agent},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class TestSchemaConstraint:
    def test_invalid_trust_state_rejected(self, db_store):
        with pytest.raises(StoreError):
            db_store.create_node(
                node_id="n1",
                kind="summary",
                tier="notes",
                trust_state="bogus",
                depth=1,
            )

    def test_valid_states_accepted(self, db_store):
        for state in ("draft", "auto-verified", "human-approved", "stale"):
            db_store.create_node(
                node_id=f"n-{state}",
                kind="summary",
                tier="notes",
                trust_state=state,
                depth=1,
            )


class TestChecksGate:
    def test_passing_checks_auto_verify(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        data = _derive(store, reg["id"])
        assert data["status"] == "derived"
        assert data["trust_state"] == "auto-verified"
        assert data["check_failures"] == []

        (state, failures) = _q(
            store, "SELECT trust_state, check_failures FROM node WHERE id=?", (data["id"],)
        )[0]
        assert state == "auto-verified"
        assert failures == "[]"

    def test_failing_checks_leave_draft_with_failures(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        data = _derive(store, reg["id"], agent=FAKE_FAILING_AGENT)
        assert data["status"] == "derived"
        assert data["trust_state"] == "draft"
        assert len(data["check_failures"]) > 0

        (state, failures) = _q(
            store, "SELECT trust_state, check_failures FROM node WHERE id=?", (data["id"],)
        )[0]
        assert state == "draft"
        assert '"Synthesis marker check failed' in failures

    def test_draft_cannot_auto_verify_without_passing_checks(self, store):
        """T6: re-running checks on a failing node must never flip it."""
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        data = _derive(store, reg["id"], agent=FAKE_FAILING_AGENT)
        assert data["trust_state"] == "draft"

        # Re-derive with a good agent — idempotency must hold, state untouched.
        again = _derive(store, reg["id"])
        assert again["status"] == "already_derived"
        (state,) = _q(store, "SELECT trust_state FROM node WHERE id=?", (data["id"],))[0]
        assert state == "draft"


class TestFailureAtomicity:
    def test_agent_failure_creates_no_node(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        result = _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), reg["id"]],
            env={"MEMEX_AGENT": FAKE_THROWS_AGENT},
        )
        # T4 — agent failure surfaces as a non-zero exit with a JSON error;
        # the derive must be atomic: no summary node, no partial state.
        assert result.returncode != 0
        assert json.loads(result.stderr)["detail"] == "Simulated LLM failure"

        summaries = _q(store, "SELECT COUNT(*) FROM node WHERE kind='summary'")
        assert summaries[0][0] == 0
        edges = _q(store, "SELECT COUNT(*) FROM edge WHERE relation='derived_from'")
        assert edges[0][0] == 1  # only the register-time extracted->url edge

    def test_derive_is_idempotent(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        first = _derive(store, reg["id"])
        second = _derive(store, reg["id"])
        assert first["status"] == "derived"
        assert second["status"] == "already_derived"
        assert second["id"] == first["id"]

        summaries = _q(store, "SELECT COUNT(*) FROM node WHERE kind='summary'")
        assert summaries[0][0] == 1
