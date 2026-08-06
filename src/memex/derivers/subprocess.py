"""Shared long-lived-subprocess client for agent wire adapters.

Extracted from ``OMPRpcAgent`` (ticket #122): one lazy process per client
instance, reused across calls, serialized turns, per-turn/startup timeouts,
stderr tail, reader loop, and atexit disposal — parameterized by a wire
adapter (argv, readiness event, prompt frame, event handling, abort) so
every agent family (omp RPC, claude stream-json, …) gets identical lifecycle
semantics without duplicating them.

The client is a plain concrete component, not an :class:`~memex.agent.Agent`:
it is not loadable via ``MEMEX_AGENT`` and knows nothing about
derive/review/extract_ideas. The shared parse helpers live here so wire
adapters reuse one implementation instead of redefining it.
"""

import atexit
import itertools
import json
import os
import re
import subprocess
import threading
import weakref
from dataclasses import dataclass, field
from typing import Callable

from memex.schemas import DocumentRef, ReviewProposal

# Stop reasons that mean "the turn produced a complete answer". Providers
# differ (deepseek: "stop", anthropic: "end_turn"); anything else — aborted,
# error, max_tokens, … — must surface as a failure, never a partial success.
CLEAN_STOP_REASONS = frozenset({"stop", "end_turn"})


@dataclass
class Turn:
    """State of one in-flight turn; the wire hooks mutate it.

    ``done`` is set by the adapter's ``on_event`` when the turn completes
    (e.g. agent_end), and by the client on timeout or process EOF. ``error``
    and ``stop_reason`` record how the turn ended; ``tool_payloads`` collects
    structured host-tool results keyed by tool name.
    """

    done: threading.Event
    id: str = ""
    text_parts: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    error: str | None = None
    tool_payloads: dict[str, dict] = field(default_factory=dict)


class SubprocessClient:
    """Lifecycle owner for one long-lived subprocess used as an LLM wire.

    Spawns the CLI lazily on the first :meth:`call_llm` (one process per
    client instance, reused for the whole batch), serializes turns, enforces
    per-turn and startup timeouts, tails stderr for diagnostics, and kills
    the process on :meth:`dispose` or at interpreter exit.

    The wire adapter supplies: ``argv`` (process command), ``is_ready``
    (classifies the readiness frame), ``send_prompt`` (writes the prompt
    frame for a turn), and ``on_event`` (interprets events and completes
    turns). Optional ``after_ready`` runs once after startup succeeds
    (e.g. host-tool registration) and ``abort`` overrides the default
    kill-on-timeout (e.g. a graceful abort frame first).
    """

    _DEFAULT_TIMEOUT = 600
    _DEFAULT_STARTUP_TIMEOUT = 30
    _MAX_SPAWNS = 2  # initial spawn + one respawn per instance
    _STDERR_TAIL_CAP = 50

    def __init__(
        self,
        *,
        name: str,  # "omp RPC" | "claude stream-json" — used in error messages
        cli_name: str,  # "omp" | "claude" — FileNotFoundError
        install_hint: str | None,
        argv: Callable[[], list[str]],
        is_ready: Callable[[dict], bool],  # classifies the readiness event
        send_prompt: Callable[[Turn, str], None],  # writes the prompt frame (uses turn.id)
        on_event: Callable[[dict, Turn], None],  # interprets events; sets done/error/stop_reason
        after_ready: Callable[[], None] | None = None,
        abort: Callable[[Turn], None] | None = None,  # default: kill the process
        clean_stop_reasons: frozenset[str],
        timeout_env: str,
        startup_timeout_env: str,
        timeout: int | None = None,
        startup_timeout: int | None = None,
    ) -> None:
        self._name = name
        self._cli_name = cli_name
        self._install_hint = install_hint
        self._argv = argv
        self._is_ready = is_ready
        self._send_prompt = send_prompt
        self._on_event = on_event
        self._after_ready = after_ready
        self._abort_hook = abort
        self._clean_stop_reasons = clean_stop_reasons
        self._timeout = timeout or int(
            os.environ.get(timeout_env, str(self._DEFAULT_TIMEOUT))
        )
        self._startup_timeout = startup_timeout or int(
            os.environ.get(startup_timeout_env, str(self._DEFAULT_STARTUP_TIMEOUT))
        )
        self._proc: subprocess.Popen | None = None
        self._current_turn: Turn | None = None
        self._ready = threading.Event()
        self._write_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._seq = itertools.count(1)
        self._spawn_count = 0
        self._stderr_tail: list[str] = []
        _LIVE.add(self)

    # ------------------------------------------------------------------
    # Agent-seam surface
    # ------------------------------------------------------------------

    @property
    def startup_timeout(self) -> int:
        """The resolved startup timeout (constructor arg or env fallback).

        Exposed for wire hooks that wait on adapter-side acks (e.g. the
        set_host_tools ack) using the same deadline as process readiness.
        """
        return self._startup_timeout

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        """Run one turn against the subprocess; return the assembled text.

        Serializes turns: the wire carries one prompt at a time. ``allow_read``
        is accepted for signature compatibility with the Agent seam; whether
        the process can read files is fixed by the adapter's argv.

        Raises RuntimeError on timeout, crash, or any stop reason other than a
        clean one — never returns a partial turn.
        """
        with self._call_lock:
            self._ensure_process()
            turn = Turn(done=threading.Event(), id=f"p{next(self._seq)}")
            self._current_turn = turn
            try:
                self._send_prompt(turn, prompt)
            except Exception as e:  # noqa: BLE001 — re-raised as RuntimeError
                self._current_turn = None
                raise RuntimeError(f"{self._name} write failed: {e}") from e

            if not turn.done.wait(self._timeout):
                self._abort(turn)
                if turn.error is None:
                    turn.error = f"{self._name} turn timed out"

            self._current_turn = None

            if turn.error:
                raise RuntimeError(turn.error)
            if turn.stop_reason not in self._clean_stop_reasons:
                raise RuntimeError(
                    f"{self._name} turn ended with stop reason: {turn.stop_reason}"
                )
            return "".join(turn.text_parts)

    def write(self, obj: dict) -> None:
        """Write one JSON frame to the process stdin (hook replies, prompts)."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError(f"{self._name} process is not running")
        with self._write_lock:
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

    def dispose(self) -> None:
        """Terminate the subprocess if running. Idempotent."""
        self._kill()
        self._current_turn = None

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def _ensure_process(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return
        if self._spawn_count >= self._MAX_SPAWNS:
            raise RuntimeError(
                f"{self._name} process crashed repeatedly; giving up "
                f"(last stderr: {' | '.join(self._stderr_tail[-3:]) or 'empty'})"
            )
        self._spawn()

    def _spawn(self) -> None:
        if self._proc is not None:
            self._kill()
        try:
            proc = subprocess.Popen(
                self._argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            hint = f" {self._install_hint}" if self._install_hint else ""
            raise RuntimeError(
                f"{self._name} requires the '{self._cli_name}' CLI.{hint}"
            ) from None
        self._proc = proc
        self._spawn_count += 1
        self._stderr_tail = []
        self._ready.clear()
        threading.Thread(
            target=self._stderr_reader, args=(proc,), daemon=True, name="subprocess-stderr"
        ).start()
        threading.Thread(
            target=self._reader, args=(proc,), daemon=True, name="subprocess-reader"
        ).start()
        if not self._ready.wait(self._startup_timeout):
            tail = self._stderr_tail[-3:]
            self._kill()
            raise RuntimeError(
                f"{self._name} did not become ready"
                + (f" (stderr: {' | '.join(tail)})" if tail else "")
            )
        if self._after_ready is not None:
            self._after_ready()

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        # Detach before killing so the reader thread's EOF handling never
        # clobbers a respawned session's state. stdout is left open: closing
        # it while the reader thread is mid-iteration raises ValueError in
        # the thread; the pipe EOFs naturally once the process dies.
        self._proc = None
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def _abort(self, turn: Turn) -> None:
        if self._abort_hook is not None:
            self._abort_hook(turn)
            return
        self._kill()
        turn.error = f"{self._name} turn timed out"
        turn.done.set()

    # ------------------------------------------------------------------
    # Reader threads
    # ------------------------------------------------------------------

    def _stderr_reader(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip("\n"))
                if len(self._stderr_tail) > self._STDERR_TAIL_CAP:
                    self._stderr_tail.pop(0)
        except Exception:  # noqa: BLE001
            pass

    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            try:
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if self._is_ready(ev):
                        self._ready.set()
                        continue
                    self._on_event(ev, self._current_turn)
            except (OSError, ValueError):
                # Pipe closed under us (dispose/respawn) — treat as EOF.
                pass
        finally:
            # EOF: the process died. Only touch shared state if this reader
            # still owns the active process (identity check guards respawns).
            if self._proc is proc:
                turn = self._current_turn
                if turn is not None and not turn.done.is_set():
                    tail = " | ".join(self._stderr_tail[-3:])
                    turn.error = (
                        f"{self._name} process exited mid-turn"
                        + (f" (stderr: {tail})" if tail else "")
                    )
                    turn.done.set()


_LIVE: weakref.WeakSet = weakref.WeakSet()


def _dispose_all() -> None:
    for client in list(_LIVE):
        client.dispose()


atexit.register(_dispose_all)

# ----------------------------------------------------------------------
# Shared wire-adapter helpers (prompt/parse) — functions, not classes, so
# every adapter reuses one implementation.
# ----------------------------------------------------------------------

_REFERENCE_TEMPLATE = (
    "# Source document\n\n"
    "- id: {node_id}\n"
    "- title: {title}\n"
    "- source_url: {source_url}\n"
    "- path: {content_path}\n"
    "- size_bytes: {size_bytes}\n"
)


def format_reference(reference: DocumentRef | list[DocumentRef]) -> str:
    """Render one or more document references as a reader-mode prompt block.

    Reader agents read each document themselves in multiple passes (read
    tool, offset/limit), so sources of any length fit without inlining.
    """
    docs = reference if isinstance(reference, list) else [reference]
    blocks = []
    for i, ref in enumerate(docs, start=1):
        blocks.append(
            f"Document {i}:\n"
            + _REFERENCE_TEMPLATE.format(
                node_id=ref.node_id,
                title=ref.title or "(no title)",
                source_url=ref.source_url or "(none)",
                content_path=ref.content_path,
                size_bytes=ref.size_bytes,
            )
        )
    return "\n\n".join(blocks)


def extract_json_object(raw: str) -> dict | None:
    """Best-effort parse of a JSON object, tolerating markdown code fences."""
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def parse_ideas_raw(raw: str) -> list[str]:
    """Parse an ideas response: a bare JSON array of strings, else [].

    Mirrors the omp fallback contract: a successful parse of a list wins;
    anything else (non-JSON, object, …) yields no ideas.
    """
    try:
        ideas = json.loads(raw)
        if isinstance(ideas, list):
            return [str(i) for i in ideas]
    except json.JSONDecodeError:
        pass
    return []


def parse_review_raw(raw: str) -> ReviewProposal:
    """Parse a review response into a ReviewProposal.

    Tries the JSON envelope first (tolerating markdown fences); on failure,
    degrades to a conservative proposal — no affected nodes, low confidence,
    the raw text as rationale. Never invents affected nodes.
    """
    data = extract_json_object(raw)
    if data is not None:
        return ReviewProposal(
            affected_node_ids=data.get("affected_node_ids", []),
            damage_boundary_node_id=data.get("damage_boundary_node_id"),
            rationale_md=data.get("rationale_md", raw),
            confidence=data.get("confidence", "low"),
        )
    return ReviewProposal(
        affected_node_ids=[],
        damage_boundary_node_id=None,
        rationale_md=raw,
        confidence="low",
    )
