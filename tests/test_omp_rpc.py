"""Hermetic tests for OMPRpcAgent against a fake `omp` RPC process.

The fake (tests/fake_omp_rpc.py) is installed on PATH as `omp`; no network,
no real LLM. Pins the invariants from docs/prd/agent-rpc-service.md:

- at most one prompt in flight per session (serialization)
- every prompt terminates with exactly one agent_end before the next starts
- call_llm returns the full turn text or raises — never a partial success
- a crashed session never serves a call: in-flight fails, session respawns once
- turn timeout always terminates the call (abort → kill)
- one process serves the whole batch (spawned once per agent instance)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memex.agent import load_agent
from memex.derivers.pi import OMPRpcAgent
from tests.conftest import _run_memex, register_node

FAKE = Path(__file__).parent / "fake_omp_rpc.py"


@pytest.fixture
def fake_omp(tmp_path, monkeypatch):
    """Install the fake `omp` on PATH; return env/marker helpers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "omp"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        f"import runpy\n"
        f"runpy.run_path({str(FAKE)!r}, run_name='__main__')\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + __import__("os").environ.get("PATH", ""))
    marker = tmp_path / "spawns.txt"
    monkeypatch.setenv("FAKE_OMP_MARKER", str(marker))
    monkeypatch.delenv("MEMEX_OMP_TIMEOUT", raising=False)
    monkeypatch.delenv("MEMEX_OMP_STARTUP_TIMEOUT", raising=False)

    def set_env(**kw):
        for k, v in kw.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, str(v))

    return {"marker": marker, "set_env": set_env}


def _spawn_count(marker: Path) -> int:
    if not marker.exists():
        return 0
    return sum(1 for line in marker.read_text().splitlines() if line == "start")


def _mk_validation_node(tmp_path: Path):
    """Minimal db: one parent node + one derivation node + provenance edge."""
    import sqlite3
    import uuid
    from datetime import datetime, timezone

    from memex.store import Store

    db_path = tmp_path / "validation.db"
    with Store.open(db_path) as store:
        store.init_schema()
        parent_id = str(uuid.uuid4())
        parent_path = tmp_path / "parent.md"
        parent_path.write_text("Parent content with facts. " * 6, encoding="utf-8")
        store.create_node(
            node_id=parent_id, kind="summary", tier="notes", depth=1,
            content_path=str(parent_path),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        node_id = str(uuid.uuid4())
        node_path = tmp_path / "node.md"
        node_path.write_text(
            "# Note\n\n"
            "This article discusses the topic with a claim.\n\n"
            "> Synthesis: The claim is wrong.\n\n"
            "The source material covers the subject thoroughly.",
            encoding="utf-8",
        )
        store.create_node(
            node_id=node_id, kind="summary", tier="notes", depth=2,
            content_path=str(node_path),
            synthesis_statements=["The claim is wrong."],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        store.create_edge(
            edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
            from_node=node_id, to_node=parent_id,
        )
    con = sqlite3.connect(db_path)
    return con, node_id, node_path


class TestRpcTurn:
    def test_assembles_text_from_deltas(self, fake_omp):
        agent = OMPRpcAgent()
        try:
            assert agent.call_llm("ping") == "Hello world"
        finally:
            agent.dispose()

    def test_reuses_process_across_calls(self, fake_omp):
        agent = OMPRpcAgent()
        try:
            agent.call_llm("one")
            agent.call_llm("two")
            assert _spawn_count(fake_omp["marker"]) == 1
        finally:
            agent.dispose()

    def test_loadable_via_memex_agent_seam(self, fake_omp):
        client = load_agent("memex.derivers.pi:OMPRpcAgent")
        assert isinstance(client, OMPRpcAgent)
        client.dispose()


class TestRpcFailure:
    def test_no_ready_raises(self, fake_omp):
        fake_omp["set_env"](FAKE_OMP_NO_READY=1)
        agent = OMPRpcAgent(startup_timeout=1)
        with pytest.raises(RuntimeError, match="did not become ready"):
            agent.call_llm("ping")

    def test_timeout_aborts_turn(self, fake_omp):
        fake_omp["set_env"](FAKE_OMP_HANG=1)
        agent = OMPRpcAgent(timeout=1, abort_grace=5)
        try:
            with pytest.raises(RuntimeError, match="turn timed out"):
                agent.call_llm("ping")
        finally:
            agent.dispose()

    def test_timeout_kills_when_abort_ignored(self, fake_omp):
        fake_omp["set_env"](FAKE_OMP_HANG=1, FAKE_OMP_IGNORE_ABORT=1)
        agent = OMPRpcAgent(timeout=1, abort_grace=1)
        try:
            with pytest.raises(RuntimeError, match="turn timed out"):
                agent.call_llm("ping")
        finally:
            agent.dispose()

    def test_crash_mid_turn_fails_and_respawns(self, fake_omp):
        fake_omp["set_env"](FAKE_OMP_CRASH_ON=1)
        agent = OMPRpcAgent()
        try:
            with pytest.raises(RuntimeError, match="exited mid-turn"):
                agent.call_llm("one")
            assert agent.call_llm("two") == "Hello world"
            assert _spawn_count(fake_omp["marker"]) == 2
        finally:
            agent.dispose()

    def test_crash_exhaustion_raises(self, fake_omp):
        fake_omp["set_env"](FAKE_OMP_CRASH_ALL=1)
        agent = OMPRpcAgent()
        try:
            with pytest.raises(RuntimeError, match="exited mid-turn"):
                agent.call_llm("one")
            with pytest.raises(RuntimeError, match="exited mid-turn"):
                agent.call_llm("two")
            with pytest.raises(RuntimeError, match="crashed repeatedly"):
                agent.call_llm("three")
            assert _spawn_count(fake_omp["marker"]) == 2
        finally:
            agent.dispose()


class TestHostTools:
    """Phase 2: structured results via set_host_tools / host_tool_call."""

    def test_tools_registered_on_spawn(self, fake_omp):
        agent = OMPRpcAgent()
        try:
            agent.call_llm("ping")
            assert "tools" in fake_omp["marker"].read_text().splitlines()
        finally:
            agent.dispose()

    def test_derive_uses_structured_payload(self, fake_omp):
        fake_omp["set_env"](
            FAKE_OMP_TOOL="submit_derivation",
            FAKE_OMP_TOOL_ARGS=json.dumps(
                {
                    "prose": "# Title\nReal prose.",
                    "synthesis_statements": ["S1", "S2"],
                }
            ),
            # Unparseable text on purpose: the payload must win.
            FAKE_OMP_TEXT="this text is NOT valid json and must be ignored",
        )
        agent = OMPRpcAgent()
        try:
            dr = agent.derive(content="source")
            assert dr.prose == "# Title\nReal prose."
            assert dr.synthesis_statements == ["S1", "S2"]
        finally:
            agent.dispose()

    def test_derive_unescapes_json_escapes_in_payload(self, fake_omp):
        """Tool-payload prose with JSON-escaped apostrophes (\\') must be
        unescaped identically on prose and statements so DB/file never drift
        (regression: D2 synthesis-marker check tripped on the raw escapes)."""
        fake_omp["set_env"](
            FAKE_OMP_TOOL="submit_derivation",
            FAKE_OMP_TOOL_ARGS=json.dumps(
                {
                    "prose": "Handbook\\'s claim and Ronacher\\'s reply.",
                    "synthesis_statements": ["Handbook\\'s claim"],
                }
            ),
        )
        agent = OMPRpcAgent()
        try:
            dr = agent.derive(content="source")
            assert dr.prose == "Handbook's claim and Ronacher's reply."
            assert dr.synthesis_statements == ["Handbook's claim"]
        finally:
            agent.dispose()

    def test_review_uses_structured_payload(self, fake_omp):
        fake_omp["set_env"](
            FAKE_OMP_TOOL="submit_review",
            FAKE_OMP_TOOL_ARGS=json.dumps(
                {
                    "affected_node_ids": ["n1", "n2"],
                    "damage_boundary_node_id": "n2",
                    "rationale_md": "Both depend on the claim.",
                    "confidence": "medium",
                }
            ),
        )
        agent = OMPRpcAgent()
        try:
            rp = agent.review("target", "asserting", {})
            assert rp.affected_node_ids == ["n1", "n2"]
            assert rp.damage_boundary_node_id == "n2"
            assert rp.confidence == "medium"
        finally:
            agent.dispose()

    def test_extract_ideas_uses_structured_payload(self, fake_omp):
        fake_omp["set_env"](
            FAKE_OMP_TOOL="submit_ideas",
            FAKE_OMP_TOOL_ARGS=json.dumps({"ideas": ["Idea 1", "Idea 2"]}),
        )
        agent = OMPRpcAgent()
        try:
            assert agent.extract_ideas(content="src") == ["Idea 1", "Idea 2"]
        finally:
            agent.dispose()

    def test_validations_use_structured_payload(self, fake_omp, tmp_path):
        """run_validations consumes the submit_verdicts host-tool payload (V1)."""
        from memex.validators.validate import run_validations

        fake_omp["set_env"](
            FAKE_OMP_TOOL="submit_verdicts",
            FAKE_OMP_TOOL_ARGS=json.dumps(
                {
                    "verdicts": [
                        {
                            "claim": "The claim is wrong.",
                            "verdict": "UNSUPPORTED",
                            "source_examined": "parent",
                            "absence_explanation": "no supporting content",
                        }
                    ]
                }
            ),
            # Unparseable text on purpose: the payload must win.
            FAKE_OMP_TEXT="this text is NOT valid json and must be ignored",
        )
        agent = OMPRpcAgent()
        try:
            con, node_id, node_path = _mk_validation_node(tmp_path)
            result = run_validations(agent, con, node_id, node_path)
            assert not result.passed
            assert any(f.startswith("V1:") for f in result.failures)
        finally:
            agent.dispose()

    def test_submit_verdicts_schema_requires_payload_fields_per_shape(self):
        """F2: submit_verdicts carries one tool with two payload shapes (V1
        verdicts array, V2 passes/reason pair), so its JSON Schema keeps a
        per-shape ``required`` list via anyOf — an LLM omitting the payload
        fields is rejected by the schema instead of degrading to
        pass-with-warning (the 'no room for maneuver' contract)."""
        from memex.derivers.pi import _HOST_TOOLS

        tool = next(t for t in _HOST_TOOLS if t["name"] == "submit_verdicts")
        params = tool["parameters"]
        branches = params.get("anyOf")
        assert branches, (
            "submit_verdicts must be a union schema (anyOf) with per-shape "
            "required lists, not an unconstrained object"
        )
        required_by_branch = [set(b.get("required", [])) for b in branches]
        assert {"verdicts"} in required_by_branch  # V1 shape
        assert {"passes"} in required_by_branch  # V2 shape
        props = set(params["properties"])
        for req in required_by_branch:
            assert req <= props, f"required fields {req} not in properties {props}"

        def _matches(payload: dict, branch: dict) -> bool:
            if not isinstance(payload, dict):
                return False
            for req in branch.get("required", []):
                if req not in payload:
                    return False
            for key, value in payload.items():
                spec = params["properties"].get(key)
                if spec is None:
                    continue
                spec_type = spec.get("type")
                if spec_type == "array":
                    if not isinstance(value, list):
                        return False
                elif spec_type == "boolean":
                    if not isinstance(value, bool):
                        return False
                elif spec_type == "string":
                    if not isinstance(value, str):
                        return False
            return True

        def _valid(payload: dict) -> bool:
            return any(_matches(payload, b) for b in branches)

        assert _valid({"verdicts": [{"claim": "c", "verdict": "SUPPORTED"}]})
        assert _valid({"passes": True, "reason": "ok"})
        assert _valid({"verdicts": [], "passes": True})  # both shapes at once
        # Omitting the payload fields never validates.
        assert not _valid({})
        assert not _valid({"reason": "ok"})  # V2 shape without passes
        assert not _valid({"verdicts": "not-a-list"})  # wrong type

    def test_fallback_to_text_when_no_tool_call(self, fake_omp):
        # No FAKE_OMP_TOOL: the fake returns plain text; derive parses it.
        fake_omp["set_env"](
            FAKE_OMP_TEXT=json.dumps(
                {
                    "prose": (
                        "This article discusses the topic at hand.\n\n"
                        "> Synthesis: S"
                    ),
                    "synthesis_statements": ["S"],
                }
            )
        )
        agent = OMPRpcAgent()
        try:
            dr = agent.derive(content="source")
            assert dr.synthesis_statements == ["S"]
        finally:
            agent.dispose()


class TestRpcDerive:
    def test_derive_end_to_end_via_cli(self, fake_omp, store):
        # Mirrors tests/fake_llm_client.py:FakeAgent.derive — passes the
        # deterministic checks (D1–D5), so the node auto-verifies.
        fake_omp["set_env"](
            FAKE_OMP_TEXT=json.dumps(
                {
                    "prose": (
                        "This article discusses the topic at hand.\n\n"
                        "> Synthesis: The author implies a broader pattern beyond "
                        "what is stated directly.\n\n"
                        "The source material covers the subject thoroughly."
                    ),
                    "synthesis_statements": [
                        "The author implies a broader pattern beyond what is stated directly."
                    ],
                }
            )
        )
        reg = json.loads(
            register_node(store, store["vault"], "rpc.md", "https://example.com/rpc").stdout
        )
        p = _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), reg["id"]],
            env={"MEMEX_AGENT": "memex.derivers.pi:OMPRpcAgent"},
        )
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert data["status"] == "derived"
        assert data["trust_state"] == "auto-verified"
