#!/usr/bin/env python3
"""Fake ``omp`` executable for hermetic OMPRpcAgent tests.

Emits the RPC-mode protocol frames on stdout and reads commands from stdin —
never touches the real omp, the network, or an LLM. Tests put this script on
PATH as ``omp``.

Behavior is driven by env vars:

  FAKE_OMP_MARKER=<path>    append "start\\n" on every process start (spawn counting)
                            and "tools\\n" when set_host_tools is received
  FAKE_OMP_TEXT=<str>       text emitted as two deltas (default "Hello world")
  FAKE_OMP_NO_READY=1       exit(1) without emitting the ready frame
  FAKE_OMP_HANG=1           never end the turn; still answer abort with
                            agent_end(stopReason=aborted)
  FAKE_OMP_IGNORE_ABORT=1   never answer abort (client must kill the process)
  FAKE_OMP_CRASH_ON=<n>     exit(3) after agent_start on the n-th *process*
                            (counted via the spawn marker, so a respawn survives)
  FAKE_OMP_CRASH_ALL=1      exit(3) after agent_start on every process
  FAKE_OMP_TOOL=<name>      on prompt, emit a host_tool_call for this tool with
                            FAKE_OMP_TOOL_ARGS (JSON), await host_tool_result,
                            then end the turn (no text deltas)
  FAKE_OMP_TOOL_ARGS=<json> arguments for the host tool call (default {})
"""

import json
import os
import sys


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    marker = os.environ.get("FAKE_OMP_MARKER")
    proc_no = 0  # 0-based ordinal of this process among all spawns
    if marker:
        if os.path.exists(marker):
            with open(marker) as fh:
                proc_no = len(fh.read().splitlines())
        with open(marker, "a") as fh:
            fh.write("start\n")

    if os.environ.get("FAKE_OMP_NO_READY"):
        sys.exit(1)

    _emit(
        {
            "type": "ready",
            "protocolVersion": 1,
            "supportedProtocolVersions": [1, 2],
            "maxFrameBytes": 1048576,
            "maxReassembledFrameBytes": 67108864,
        }
    )

    text = os.environ.get("FAKE_OMP_TEXT", "Hello world")
    crash_on = int(os.environ.get("FAKE_OMP_CRASH_ON", "0") or "0")
    crash_all = bool(os.environ.get("FAKE_OMP_CRASH_ALL"))
    hang = bool(os.environ.get("FAKE_OMP_HANG"))
    ignore_abort = bool(os.environ.get("FAKE_OMP_IGNORE_ABORT"))
    tool_name = os.environ.get("FAKE_OMP_TOOL")
    tool_args = json.loads(os.environ.get("FAKE_OMP_TOOL_ARGS") or "{}")

    def _end_turn():
        # Mirrors the real wire (omp 17.2.9): the stop reason rides on the
        # message envelope, not on agent_end.
        _emit(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        )
        _emit({"type": "agent_end", "messages": []})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        ctype = cmd.get("type")
        cid = cmd.get("id")

        if ctype == "set_host_tools":
            if marker:
                with open(marker, "a") as fh:
                    fh.write("tools\n")
            names = [t.get("name") for t in (cmd.get("tools") or [])]
            _emit(
                {
                    "id": cid,
                    "type": "response",
                    "command": "set_host_tools",
                    "success": True,
                    "data": {"toolNames": names},
                }
            )

        elif ctype == "prompt":
            _emit({"id": cid, "type": "response", "command": "prompt", "success": True})
            _emit({"type": "agent_start"})
            if crash_all or (crash_on and proc_no + 1 == crash_on):
                sys.exit(3)
            if hang:
                # Stay alive and keep serving commands; never end the turn.
                continue
            if tool_name:
                _emit(
                    {
                        "type": "host_tool_call",
                        "id": "f1",
                        "toolCallId": "tc1",
                        "toolName": tool_name,
                        "arguments": tool_args,
                    }
                )
                # Await the host's host_tool_result before ending the turn.
                for line in sys.stdin:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        reply = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if reply.get("type") == "host_tool_result":
                        break
                _end_turn()
                continue
            mid = len(text) // 2
            _emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": text[:mid]},
                }
            )
            _emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": text[mid:]},
                }
            )
            _end_turn()

        elif ctype == "abort":
            _emit({"id": cid, "type": "response", "command": "abort", "success": True})
            if not ignore_abort:
                _emit({"type": "agent_end", "stopReason": "aborted"})

        elif ctype == "extension_ui_response":
            pass

        elif ctype in ("steer", "compact", "set_model", "set_thinking_level"):
            _emit({"id": cid, "type": "response", "command": ctype, "success": True})

        # Anything else: ignore.


if __name__ == "__main__":
    main()
