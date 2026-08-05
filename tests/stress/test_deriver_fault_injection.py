"""Stress — deriver/agent seam fault injection.

F1  persistent agent failure -> status=error, exit non-zero, atomic (no node)
F2  transient agent failure recovers through the retry path (derive --all)
F3  out-of-schema agent response (dict, not DerivationResult) -> hard error,
    no partial state
F4  a derivation that passes checks after retry reaches auto-verified
F5  synthesize confidence: min of parents' confidence; high+high must not
    degrade to low (campaign finding — RED on current code)
"""
from __future__ import annotations

import json
import sqlite3

from tests.conftest import _run_memex, register_node

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_THROWS_AGENT = "tests.fake_llm_client_throws:FakeLLMClientThrows"
FAKE_MALFORMED_AGENT = "tests.fake_llm_client_malformed:FakeLLMClientMalformed"
FAKE_FLAKY_AGENT = "tests.fake_llm_client_flaky:FakeLLMClientFlaky"


def _q(store, sql: str, params: tuple = ()) -> list:
    con = sqlite3.connect(str(store["db"]))
    try:
        rows = con.execute(sql, params).fetchall()
        con.commit()  # writes from stress tests must persist for later reads
        return rows
    finally:
        con.close()


def _derive(store, node_id: str, agent: str = FAKE_AGENT):
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": agent},
    )


def _derive_all(store, agent: str = FAKE_AGENT, limit: int | None = None):
    args = ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), "--all"]
    if limit is not None:
        args.extend(["--limit", str(limit)])
    return _run_memex(args, env={"MEMEX_AGENT": agent})


def _synthesize(store, *node_ids: str, agent: str = FAKE_AGENT):
    return _run_memex(
        ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), *node_ids],
        env={"MEMEX_AGENT": agent},
    )


class TestFaultInjection:
    def test_persistent_failure_is_atomic(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        result = _derive(store, reg["id"], agent=FAKE_THROWS_AGENT)
        assert result.returncode != 0
        assert json.loads(result.stderr)["detail"] == "Simulated LLM failure"
        assert _q(store, "SELECT COUNT(*) FROM node WHERE kind='summary'")[0][0] == 0

    def test_out_of_schema_response_is_atomic(self, store):
        reg = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        result = _derive(store, reg["id"], agent=FAKE_MALFORMED_AGENT)
        assert result.returncode != 0
        data = json.loads(result.stderr)
        assert data["error"] == "error"
        # F3 — the malformed response must never create a node or edge.
        assert _q(store, "SELECT COUNT(*) FROM node WHERE kind='summary'")[0][0] == 0
        assert _q(store, "SELECT COUNT(*) FROM edge WHERE relation='derived_from'")[0][0] == 1

    def test_batch_retry_recovers_from_transient_failures(self, store):
        register_node(store, store["vault"], "a.md", "https://example.com/a")
        result = _derive_all(store, agent=FAKE_FLAKY_AGENT)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data[0]["status"] == "derived"
        # F4 — retry recovered; the derivation passed checks.
        (state,) = _q(store, "SELECT trust_state FROM node WHERE id=?", (data[0]["id"],))[0]
        assert state == "auto-verified"

    def test_batch_persistent_failure_reported_not_crashed(self, store):
        register_node(store, store["vault"], "a.md", "https://example.com/a")
        result = _derive_all(store, agent=FAKE_THROWS_AGENT)
        assert result.returncode == 0, result.stderr  # batch never raises
        data = json.loads(result.stdout)
        assert data[0]["status"] == "error"
        assert _q(store, "SELECT COUNT(*) FROM node WHERE kind='summary'")[0][0] == 0


class TestSynthesisConfidence:
    def test_synthesis_confidence_is_min_of_parents(self, store):
        """F5 — campaign finding: high+high parents must stay high.

        Current code degrades all-high parents to 'low' (the else branch of
        the min() chain). This test is RED until Phase 4.
        """
        a = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        b = json.loads(register_node(store, store["vault"], "b.md", "https://example.com/b").stdout)
        da = _derive(store, a["id"])
        db = _derive(store, b["id"])
        assert json.loads(da.stdout)["status"] == "derived"
        assert json.loads(db.stdout)["status"] == "derived"

        # Promote both parents to high (simulating 2+ parents / pdf sources).
        for node_id in (json.loads(da.stdout)["id"], json.loads(db.stdout)["id"]):
            _q(store, "UPDATE node SET confidence='high' WHERE id=?", (node_id,))

        s = _synthesize(store, json.loads(da.stdout)["id"], json.loads(db.stdout)["id"])
        assert json.loads(s.stdout)["status"] == "synthesized"
        (conf,) = _q(store, "SELECT confidence FROM node WHERE id=?", (json.loads(s.stdout)["id"],))[0]
        assert conf == "high", f"high+high synthesis degraded to {conf!r}"

    def test_synthesis_confidence_medium_when_parent_medium(self, store):
        a = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
        b = json.loads(register_node(store, store["vault"], "b.md", "https://example.com/b").stdout)
        da = _derive(store, a["id"])
        db = _derive(store, b["id"])
        s = _synthesize(store, json.loads(da.stdout)["id"], json.loads(db.stdout)["id"])
        (conf,) = _q(store, "SELECT confidence FROM node WHERE id=?", (json.loads(s.stdout)["id"],))[0]
        assert conf == "medium"
