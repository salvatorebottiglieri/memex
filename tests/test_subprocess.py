"""Hermetic tests for the shared SubprocessClient component.

Pins the extraction contract from ticket #122:

- SubprocessClient is a plain concrete component, NOT an Agent: it has no
  derive/review/extract_ideas methods, so ``load_agent`` must reject it.
- The lifecycle (lazy spawn, reuse across calls, reader loop, dispose) works
  against a generic wire adapter — the same hooks OMPRpcAgent provides.

The wire here is a tiny stub protocol (ready / text / end frames), exercising
the client generically rather than through the omp-specific fake.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memex.agent import Agent, load_agent
from memex.derivers.subprocess import SubprocessClient, Turn

STUB_WIRE = r'''
#!/usr/bin/env python3
"""Minimal generic wire for SubprocessClient tests.

Protocol: emit {"type": "ready"} at start; then for each
{"type": "prompt", "message": ...} emit a text frame and an end frame.
Appends "start\\n" to FAKE_SUBPROCESS_MARKER (if set) on every spawn.
"""
import json
import os
import sys


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    marker = os.environ.get("FAKE_SUBPROCESS_MARKER")
    if marker:
        with open(marker, "a") as fh:
            fh.write("start\n")
    _emit({"type": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cmd.get("type") == "prompt":
            _emit({"type": "text", "delta": "stub reply to " + cmd["message"]})
            _emit({"type": "end", "stopReason": "stop"})


if __name__ == "__main__":
    main()
'''


def _on_event(ev: dict, turn: Turn) -> None:
    """Stub wire handler: text frames append, end frames finish the turn."""
    t = ev.get("type")
    if t == "text":
        turn.text_parts.append(ev.get("delta") or "")
    elif t == "end":
        turn.stop_reason = ev.get("stopReason")
        turn.done.set()


def _make_client(
    wire: Path,
    *,
    timeout: int = 10,
    startup_timeout: int = 10,
) -> SubprocessClient:
    holder: dict[str, SubprocessClient] = {}

    def send_prompt(turn: Turn, prompt: str) -> None:
        holder["client"].write({"type": "prompt", "message": prompt})

    client = SubprocessClient(
        name="stub",
        cli_name="stub-wire",
        install_hint="Run the stub wire",
        argv=lambda: [sys.executable, str(wire)],
        is_ready=lambda ev: ev.get("type") == "ready",
        send_prompt=send_prompt,
        on_event=_on_event,
        clean_stop_reasons=frozenset({"stop", "end_turn"}),
        timeout_env="MEMEX_STUB_TIMEOUT",
        startup_timeout_env="MEMEX_STUB_STARTUP_TIMEOUT",
        timeout=timeout,
        startup_timeout=startup_timeout,
    )
    holder["client"] = client
    return client


@pytest.fixture
def stub_wire(tmp_path, monkeypatch) -> Path:
    script = tmp_path / "stub_wire.py"
    script.write_text(STUB_WIRE, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.delenv("MEMEX_STUB_TIMEOUT", raising=False)
    monkeypatch.delenv("MEMEX_STUB_STARTUP_TIMEOUT", raising=False)
    return script


class TestNotAnAgent:
    def test_subprocess_client_is_not_an_agent(self, stub_wire):
        client = _make_client(stub_wire)
        try:
            assert not isinstance(client, Agent)
            assert not hasattr(client, "derive")
            assert not hasattr(client, "review")
            assert not hasattr(client, "extract_ideas")
        finally:
            client.dispose()

    def test_load_agent_rejects_subprocess_client(self):
        # load_class instantiates with no args; SubprocessClient requires
        # keyword-only wire hooks, so loading it must fail.
        with pytest.raises((TypeError, ImportError)):
            load_agent("memex.derivers.subprocess:SubprocessClient")


class TestLifecycleSmoke:
    def test_spawn_prompt_text_dispose(self, stub_wire):
        client = _make_client(stub_wire)
        try:
            assert client.call_llm("hello") == "stub reply to hello"
        finally:
            client.dispose()

    def test_reuses_process_across_calls(self, stub_wire, tmp_path, monkeypatch):
        marker = tmp_path / "spawns.txt"
        monkeypatch.setenv("FAKE_SUBPROCESS_MARKER", str(marker))
        client = _make_client(stub_wire)
        try:
            client.call_llm("one")
            client.call_llm("two")
            assert marker.read_text().splitlines() == ["start"]
        finally:
            client.dispose()
