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
    return len(marker.read_text().splitlines()) if marker.exists() else 0


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
