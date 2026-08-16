"""Hermetic tests for ClaudeCodeAgent against a fake `claude` stream-json process.

The fake (tests/fake_claude_code.py) is installed on PATH as `claude`; no
network, no real LLM. Pins the wire invariants from ticket #123:

- text comes from the last non-empty assistant snapshot (--verbose, last-wins)
- clean stop = result subtype in {success, result} with is_error falsy;
  error subtypes surface the subtype in the RuntimeError
- the legacy {"type": "system", "subtype": "result"} frame resolves exactly
  like the modern result frame; a legacy frame marked is_error fails the turn
- empty assistant text on a clean result falls back to the double-encoded
  result.result field
- control_request (can_use_tool) is answered with a deny control_response
- one process serves the whole batch (spawned once per agent instance)
- a crashed session never serves a call: in-flight fails, session respawns once
- turn timeout always terminates the call, never hangs
- derive/extract_ideas/review parse the text path (no host tools on this wire)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memex.agent import load_agent
from memex.derivers.claude_code import ClaudeCodeAgent
from tests.conftest import _run_memex, register_node

FAKE = Path(__file__).parent / "fake_claude_code.py"


@pytest.fixture
def fake_cc(tmp_path, monkeypatch):
    """Install the fake `claude` on PATH; return env/marker helpers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        f"import runpy\n"
        f"runpy.run_path({str(FAKE)!r}, run_name='__main__')\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv(
        "PATH", str(bin_dir) + ":" + __import__("os").environ.get("PATH", "")
    )
    marker = tmp_path / "spawns.txt"
    monkeypatch.setenv("FAKE_CC_MARKER", str(marker))
    monkeypatch.delenv("MEMEX_CC_TIMEOUT", raising=False)
    monkeypatch.delenv("MEMEX_CC_STARTUP_TIMEOUT", raising=False)
    monkeypatch.delenv("MEMEX_CC_PERMISSION_MODE", raising=False)
    # Validation is always-on now (no MEMEX_VALIDATOR to unset): the fake
    # claude shim answers the judge turns deterministically — V1/V2 get the
    # configured FAKE_CC_TEXT, which fails the structured-verdict parse and
    # degrades to pass-with-warning, so tests stay hermetic and green.

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


class TestCcTurn:
    def test_assembles_text_from_assistant_snapshot(self, fake_cc):
        agent = ClaudeCodeAgent()
        try:
            assert agent.call_llm("ping") == "Hello world"
        finally:
            agent.dispose()

    def test_reuses_process_across_calls(self, fake_cc):
        agent = ClaudeCodeAgent()
        try:
            agent.call_llm("one")
            agent.call_llm("two")
            assert _spawn_count(fake_cc["marker"]) == 1
        finally:
            agent.dispose()

    def test_loadable_via_memex_agent_seam(self, fake_cc):
        client = load_agent("memex.derivers.claude_code:ClaudeCodeAgent")
        assert isinstance(client, ClaudeCodeAgent)
        client.dispose()

    def test_last_assistant_snapshot_wins(self, fake_cc):
        # Two snapshots: "first " then the full text. --verbose carries the
        # accumulated message, so the last non-empty snapshot must REPLACE,
        # not append.
        fake_cc["set_env"](FAKE_CC_MULTI_ASSISTANT=1)
        agent = ClaudeCodeAgent()
        try:
            assert agent.call_llm("ping") == "Hello world"
        finally:
            agent.dispose()

    def test_legacy_result_frame_assembles_text(self, fake_cc):
        # The legacy {"type": "system", "subtype": "result"} frame with
        # is_error false must resolve exactly like the modern "result" frame.
        fake_cc["set_env"](FAKE_CC_LEGACY_RESULT=1)
        agent = ClaudeCodeAgent()
        try:
            assert agent.call_llm("ping") == "Hello world"
        finally:
            agent.dispose()

    def test_empty_result_falls_back_to_double_encoded_result(self, fake_cc):
        # No assistant text; the clean result carries the double-encoded
        # answer in result.result.
        fake_cc["set_env"](FAKE_CC_EMPTY_RESULT=1)
        agent = ClaudeCodeAgent()
        try:
            assert agent.call_llm("ping") == "Hello world"
        finally:
            agent.dispose()

    def test_control_request_denied_and_turn_completes(self, fake_cc):
        fake_cc["set_env"](FAKE_CC_CONTROL_REQUEST=1)
        agent = ClaudeCodeAgent(timeout=10)
        try:
            assert agent.call_llm("ping") == "Hello world"
            assert "deny" in fake_cc["marker"].read_text().splitlines()
        finally:
            agent.dispose()


class TestCcFailure:
    def test_no_init_raises(self, fake_cc):
        fake_cc["set_env"](FAKE_CC_NO_INIT=1)
        agent = ClaudeCodeAgent(startup_timeout=1)
        with pytest.raises(RuntimeError, match="did not become ready"):
            agent.call_llm("ping")

    def test_timeout_kills_turn(self, fake_cc):
        fake_cc["set_env"](FAKE_CC_HANG=1)
        agent = ClaudeCodeAgent(timeout=1)
        try:
            with pytest.raises(RuntimeError, match="turn timed out"):
                agent.call_llm("ping")
        finally:
            agent.dispose()

    def test_crash_mid_turn_fails_and_respawns(self, fake_cc):
        fake_cc["set_env"](FAKE_CC_CRASH_ON=1)
        agent = ClaudeCodeAgent()
        try:
            with pytest.raises(RuntimeError, match="exited mid-turn"):
                agent.call_llm("one")
            assert agent.call_llm("two") == "Hello world"
            assert _spawn_count(fake_cc["marker"]) == 2
        finally:
            agent.dispose()

    def test_crash_exhaustion_raises(self, fake_cc):
        fake_cc["set_env"](FAKE_CC_CRASH_ALL=1)
        agent = ClaudeCodeAgent()
        try:
            with pytest.raises(RuntimeError, match="exited mid-turn"):
                agent.call_llm("one")
            with pytest.raises(RuntimeError, match="exited mid-turn"):
                agent.call_llm("two")
            with pytest.raises(RuntimeError, match="crashed repeatedly"):
                agent.call_llm("three")
            assert _spawn_count(fake_cc["marker"]) == 2
        finally:
            agent.dispose()

    def test_legacy_result_frame_error_raises(self, fake_cc):
        # A legacy frame marked is_error must fail the turn, never parse.
        fake_cc["set_env"](
            FAKE_CC_LEGACY_RESULT=1, FAKE_CC_LEGACY_RESULT_ERROR=1
        )
        agent = ClaudeCodeAgent()
        try:
            with pytest.raises(RuntimeError, match="result subtype: result"):
                agent.call_llm("ping")
        finally:
            agent.dispose()

    def test_error_subtype_raises_with_subtype(self, fake_cc):
        fake_cc["set_env"](FAKE_CC_RESULT_SUBTYPE="error_max_turns")
        agent = ClaudeCodeAgent()
        try:
            with pytest.raises(RuntimeError, match="error_max_turns"):
                agent.call_llm("ping")
        finally:
            agent.dispose()


class TestCcTextPaths:
    """No host tools on this wire: derive/ideas/review parse the text path."""

    def test_derive_parses_json_envelope(self, fake_cc):
        fake_cc["set_env"](
            FAKE_CC_TEXT=json.dumps(
                {"prose": "# Title\nProse.", "synthesis_statements": ["S1"]}
            )
        )
        agent = ClaudeCodeAgent()
        try:
            dr = agent.derive(content="source")
            assert dr.prose == "# Title\nProse."
            assert dr.synthesis_statements == ["S1"]
        finally:
            agent.dispose()

    def test_extract_ideas_parses_json_array(self, fake_cc):
        fake_cc["set_env"](FAKE_CC_TEXT=json.dumps(["Idea 1", "Idea 2"]))
        agent = ClaudeCodeAgent()
        try:
            assert agent.extract_ideas(content="src") == ["Idea 1", "Idea 2"]
        finally:
            agent.dispose()

    def test_review_parses_json_object(self, fake_cc):
        fake_cc["set_env"](
            FAKE_CC_TEXT=json.dumps(
                {
                    "affected_node_ids": ["n1", "n2"],
                    "damage_boundary_node_id": "n2",
                    "rationale_md": "Both depend on the claim.",
                    "confidence": "medium",
                }
            )
        )
        agent = ClaudeCodeAgent()
        try:
            rp = agent.review("target", "asserting", {})
            assert rp.affected_node_ids == ["n1", "n2"]
            assert rp.damage_boundary_node_id == "n2"
            assert rp.rationale_md == "Both depend on the claim."
            assert rp.confidence == "medium"
        finally:
            agent.dispose()


class TestCcDeriveCli:
    def test_derive_end_to_end_via_cli(self, fake_cc, store):
        # Mirrors tests/fake_llm_client.py:FakeAgent.derive — passes the
        # deterministic checks (D1-D5), so the node auto-verifies.
        fake_cc["set_env"](
            FAKE_CC_TEXT=json.dumps(
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
            register_node(
                store, store["vault"], "cc.md", "https://example.com/cc"
            ).stdout
        )
        p = _run_memex(
            ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), reg["id"]],
            env={"MEMEX_AGENT": "memex.derivers.claude_code:ClaudeCodeAgent"},
        )
        assert p.returncode == 0, p.stderr
        data = json.loads(p.stdout)
        assert data["status"] == "derived"
        assert data["trust_state"] == "auto-verified"
