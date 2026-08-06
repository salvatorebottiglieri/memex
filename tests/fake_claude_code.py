#!/usr/bin/env python3
"""Fake ``claude`` executable for hermetic ClaudeCodeAgent tests.

Emits Claude Code stream-json frames on stdout and reads prompts from stdin —
never touches the real claude CLI, the network, or an LLM. Tests put this
script on PATH as ``claude``.

Behavior is driven by env vars:

  FAKE_CC_MARKER=<path>         append "start\\n" on every process start (spawn counting)
  FAKE_CC_TEXT=<str>            assistant text (default "Hello world")
  FAKE_CC_NO_INIT=1             exit(1) without emitting the system/init frame
  FAKE_CC_HANG=1                never emit a result frame
  FAKE_CC_CRASH_ON=<n>          exit(3) after the n-th *process* receives its first
                                user message (counted via the spawn marker, so a
                                respawn survives)
  FAKE_CC_CRASH_ALL=1           exit(3) after the first user message on every process
  FAKE_CC_RESULT_SUBTYPE=<s>    result frame subtype (default "success")
  FAKE_CC_EMPTY_RESULT=1        no assistant text; result.result holds the
                                double-encoded FAKE_CC_TEXT
  FAKE_CC_MULTI_ASSISTANT=1     two assistant snapshots: first "first ", then the
                                full FAKE_CC_TEXT (pins last-wins assembly)
  FAKE_CC_CONTROL_REQUEST=1     after a user message emit a can_use_tool
                                control_request and WAIT for a control_response
                                before continuing (writes "deny\\n" to the marker
                                when the response denies the tool)
  FAKE_CC_LEGACY_RESULT=1       emit the result as the legacy
                                {"type": "system", "subtype": "result"} frame
                                instead of the top-level "result" frame
  FAKE_CC_LEGACY_RESULT_ERROR=1 with FAKE_CC_LEGACY_RESULT: mark the legacy
                                frame is_error=true so the turn must fail
"""

import json
import os
import sys


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _spawn_ordinal(marker: str) -> int:
    """0-based ordinal of this process among all spawns (marker counts prior starts)."""
    if not os.path.exists(marker):
        return 0
    with open(marker) as fh:
        return len(fh.read().splitlines())


def main() -> None:
    marker = os.environ.get("FAKE_CC_MARKER")
    proc_no = 0  # 0-based ordinal of this process among all spawns
    if marker:
        proc_no = _spawn_ordinal(marker)  # count prior starts, then record this one
        with open(marker, "a") as fh:
            fh.write("start\n")

    if os.environ.get("FAKE_CC_NO_INIT"):
        sys.exit(1)

    _emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": "",
            "model": "fake-claude",
            "tools": [{"name": "Read"}],
        }
    )

    text = os.environ.get("FAKE_CC_TEXT", "Hello world")
    crash_on = int(os.environ.get("FAKE_CC_CRASH_ON", "0") or "0")
    crash_all = bool(os.environ.get("FAKE_CC_CRASH_ALL"))
    hang = bool(os.environ.get("FAKE_CC_HANG"))
    result_subtype = os.environ.get("FAKE_CC_RESULT_SUBTYPE", "success")
    empty_result = bool(os.environ.get("FAKE_CC_EMPTY_RESULT"))
    multi_assistant = bool(os.environ.get("FAKE_CC_MULTI_ASSISTANT"))
    control_request = bool(os.environ.get("FAKE_CC_CONTROL_REQUEST"))
    legacy_result = bool(os.environ.get("FAKE_CC_LEGACY_RESULT"))
    legacy_result_error = bool(os.environ.get("FAKE_CC_LEGACY_RESULT_ERROR"))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if frame.get("type") != "user":
            continue

        if crash_all or (crash_on and proc_no + 1 == crash_on):
            sys.exit(3)

        if control_request:
            _emit(
                {
                    "type": "control_request",
                    "request_id": "cr1",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "Read",
                        "tool_input": {"file_path": "/tmp/x.md", "offset": 1, "limit": 100},
                    },
                }
            )
            # Await the host's control_response before continuing. EOF (host
            # killed us / never replied) or a non-deny answer aborts without
            # a result, so the turn fails fast instead of hanging.
            while True:
                line = sys.stdin.readline()
                if not line:
                    sys.exit(5)
                line = line.strip()
                if not line:
                    continue
                try:
                    reply = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if reply.get("type") != "control_response":
                    continue
                resp = reply.get("response") or {}
                inner = resp.get("response") or {}
                if resp.get("subtype") == "success" and inner.get("behavior") == "deny":
                    if marker:
                        with open(marker, "a") as fh:
                            fh.write("deny\n")
                else:
                    sys.exit(4)
                break

        if hang:
            # Stay alive and keep serving prompts; never end the turn.
            continue

        if multi_assistant:
            _emit(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first "}],
                        "stop_reason": None,
                    },
                }
            )

        if not empty_result:
            _emit(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                        "stop_reason": None,
                    },
                }
            )

        if legacy_result:
            _emit(
                {
                    "type": "system",
                    "subtype": "result",
                    "is_error": legacy_result_error,
                    "result": json.dumps(text),
                }
            )
        else:
            _emit(
                {
                    "type": "result",
                    "subtype": result_subtype,
                    "is_error": bool(result_subtype not in ("success", "result")),
                    "result": json.dumps(text),
                }
            )


if __name__ == "__main__":
    main()
