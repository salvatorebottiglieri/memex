"""Agent backed by the omp CLI in RPC mode.

``omp --mode rpc`` is the protocol the omp SDK documents for non-Node hosts:
a long-lived process exchanging newline-delimited JSON over stdio. One process
is spawned per memex invocation (lazily, on the first call), reused across the
whole batch, and disposed at exit — the engine boots once instead of once per
call (ADR-0017).

Reader mode: the agent reads source documents itself via the read tool, so
``derive``/``extract_ideas`` receive a :class:`DocumentRef` instead of inlined
content — sources of any length fit without a prompt cap.
"""

import atexit
import itertools
import json as _json
import os
import re as _re
import subprocess
import threading
import weakref
from dataclasses import dataclass, field

from memex.agent import Agent
from memex.schemas import DerivationResult, DocumentRef, ReviewProposal
from memex.utils.parsing import parse_derive_response

_DERIVE_SYSTEM_PROMPT = (
    "You are a research analysis assistant. Given a user's source material, produce a "
    "structured derivation note following these rules:\n"
    "1. Start with a single top-level heading (#) carrying the note's title.\n"
    "2. Write body prose that summarises the source. Facts restated from the source "
    "are unadorned; any statement that goes beyond what the source says must be "
    "marked as a synthesis statement.\n"
    "3. End with a ## Synthesis section whose body is one or more bullet points, "
    "each of the form \"> Synthesis: <inference>\". There MUST be at least one "
    "such statement. The exact prefix '> Synthesis:' is required.\n"
    "4. Return your response as a JSON object with keys: 'prose' (the full markdown), "
    "'synthesis_statements' (list of strings, each without the '> Synthesis:' prefix)."
)

_DERIVE_USER_TEMPLATE = "# Source material\n\n{content}\n"

# Reader mode: the agent reads the referenced document itself in multiple
# passes (read tool, offset/limit), so sources of any length fit without
# inlining or a prompt cap. Survey pass -> content passes -> derivation.
_DERIVE_READER_SYSTEM_PROMPT = (
    "You are a research analysis assistant. One or more source documents are "
    "available at the paths listed below. READ EVERY DOCUMENT YOURSELF, fully, in "
    "multiple passes with the read tool, then produce a structured derivation note "
    "following these rules:\n"
    "1. Survey pass: read the beginning of each document (read tool, offset=1) to "
    "grasp structure; use each document's size to plan the number of passes.\n"
    "2. Content passes: read each WHOLE document in chunks of ~150 lines (read tool: "
    "1-based offset/limit) until you have covered it end to end. Never skip the middle.\n"
    "3. Read ONLY the listed files — never any other file, never any other tool.\n"
    "4. Start with a single top-level heading (#) carrying the note's title.\n"
    "5. Write body prose that summarises the source. Facts restated from the source "
    "are unadorned; any statement that goes beyond what the source says must be "
    "marked as a synthesis statement.\n"
    "6. End with a ## Synthesis section whose body is one or more bullet points, "
    "each of the form \"> Synthesis: <inference>\". There MUST be at least one "
    "such statement. The exact prefix '> Synthesis:' is required.\n"
    "7. Return your response as a JSON object with keys: 'prose' (the full markdown), "
    "'synthesis_statements' (list of strings, each without the '> Synthesis:' prefix)."
)

_DERIVE_READER_USER_TEMPLATE = (
    "# Source document\n\n"
    "- id: {node_id}\n"
    "- title: {title}\n"
    "- source_url: {source_url}\n"
    "- path: {content_path}\n"
    "- size_bytes: {size_bytes}\n"
)

_READER_IDEAS_PROMPT = (
    "You are an ideas extractor. A source document is available at the path below. "
    "READ IT YOURSELF with the read tool (multiple passes, offset/limit) — never "
    "other files or tools — then extract 3-5 key ideas.\n\n"
    "Return ONLY a JSON array of strings, no other text.\n\n"
    "# Source document\n\n"
    "- id: {node_id}\n"
    "- title: {title}\n"
    "- source_url: {source_url}\n"
    "- path: {content_path}\n"
    "- size_bytes: {size_bytes}\n"
)

_REVIEW_SYSTEM_PROMPT = (
    "You are a review assistant for a knowledge-graph system. Given two pieces of content "
    "(the 'target' node and the 'asserting' edge or node) and metadata about the "
    "potentially-changing claim, determine which nodes materially depend on the contested claim.\n\n"
    "Return a JSON object with these fields:\n"
    '  "affected_node_ids": array of strings — IDs of nodes that materially depend on the contested claim\n'
    '  "damage_boundary_node_id": string or null — the deepest affected node, or null if none\n'
    '  "rationale_md": string — markdown explaining your reasoning\n'
    '  "confidence": string — one of "high", "medium", or "low"\n'
    "\n"
    "Respond with ONLY the JSON object itself — no markdown code fences, no commentary, "
    "and never attempt tool calls. If you cannot determine affected nodes, return "
    '{"affected_node_ids": [], "damage_boundary_node_id": null, "rationale_md": "<explanation>", "confidence": "low"}.\n'
)


def _extract_json_object(raw: str) -> dict | None:
    """Best-effort parse of a JSON object, tolerating markdown code fences."""
    stripped = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=_re.S)
    try:
        return _json.loads(stripped)
    except _json.JSONDecodeError:
        return None


# Stop reasons that mean "the turn produced a complete answer". Providers
# differ (deepseek: "stop", anthropic: "end_turn"); anything else — aborted,
# error, max_tokens, … — must surface as a failure, never a partial success.
_CLEAN_STOP_REASONS = frozenset({"stop", "end_turn"})


@dataclass
class _Turn:
    """State of one in-flight RPC prompt."""

    id: str
    done: threading.Event = field(default_factory=threading.Event)
    text_parts: list[str] = field(default_factory=list)
    stop_reason: str | None = None
    error: str | None = None


class OMPRpcAgent(Agent):
    """Agent powered by OMP (Oh My Pi — ``@nicedoc/oh-my-pi``) over RPC mode.

    Spawns ``omp --mode rpc --no-session --tools=read`` lazily on the first call
    and reuses it for the lifetime of this object (one process per memex
    invocation — the engine boots once per batch, not once per node).

    Supports any provider/model configured in ``omp`` (e.g. Claude, GPT, Gemini,
    DeepSeek). Requires ``omp`` to be installed and available on PATH.

    Usage: ``MEMEX_AGENT=memex.derivers.pi:OMPRpcAgent``
    """

    _cli_cmd = "omp"
    can_read_files = True

    _DEFAULT_TIMEOUT = 600
    _DEFAULT_STARTUP_TIMEOUT = 30
    _ABORT_GRACE = 10
    _MAX_SPAWNS = 2  # initial spawn + one respawn per instance

    def __init__(
        self,
        *,
        timeout: int | None = None,
        startup_timeout: int | None = None,
        abort_grace: int | None = None,
    ) -> None:
        self._timeout = timeout or int(
            os.environ.get("MEMEX_OMP_TIMEOUT", str(self._DEFAULT_TIMEOUT))
        )
        self._startup_timeout = startup_timeout or int(
            os.environ.get("MEMEX_OMP_STARTUP_TIMEOUT", str(self._DEFAULT_STARTUP_TIMEOUT))
        )
        self._abort_grace = (
            abort_grace if abort_grace is not None else self._ABORT_GRACE
        )
        self._proc: subprocess.Popen | None = None
        self._current_turn: _Turn | None = None
        self._last_stop_reason: str | None = None
        self._ready = threading.Event()
        self._write_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._seq = itertools.count(1)
        self._spawn_count = 0
        self._stderr_tail: list[str] = []
        _LIVE.add(self)

    # ------------------------------------------------------------------
    # Agent seam
    # ------------------------------------------------------------------

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        """Run one turn against the RPC process; return the assembled text.

        The process always runs with the read tool (reader mode), so
        ``allow_read`` is accepted for signature compatibility with the seam
        but does not change process flags. Serializes turns: the wire carries
        one prompt at a time.

        Raises RuntimeError on timeout, crash, or any stop reason other than a
        clean ``end_turn`` — never returns a partial turn.
        """
        with self._call_lock:
            self._ensure_process()
            turn = _Turn(id=f"p{next(self._seq)}")
            self._current_turn = turn
            try:
                self._write({"id": turn.id, "type": "prompt", "message": prompt})
            except Exception as e:  # noqa: BLE001 — re-raised as RuntimeError
                self._current_turn = None
                raise RuntimeError(f"omp RPC write failed: {e}") from e

            if not turn.done.wait(self._timeout):
                self._abort(turn)
                if turn.error is None:
                    turn.error = "omp turn timed out"

            self._current_turn = None

            if turn.error:
                raise RuntimeError(turn.error)
            if turn.stop_reason not in _CLEAN_STOP_REASONS:
                raise RuntimeError(
                    f"omp turn ended with stop reason: {turn.stop_reason}"
                )
            return "".join(turn.text_parts)

    def derive(
        self,
        content: str | None = None,
        *,
        reference: DocumentRef | list[DocumentRef] | None = None,
    ) -> DerivationResult:
        if reference is not None:
            prompt = (
                _DERIVE_READER_SYSTEM_PROMPT
                + "\n\n"
                + self._format_reference(reference)
            )
            raw = self.call_llm(prompt, allow_read=True)
        else:
            prompt = (
                _DERIVE_SYSTEM_PROMPT
                + "\n\n"
                + _DERIVE_USER_TEMPLATE.format(content=content or "")
            )
            raw = self.call_llm(prompt)
        prose, statements = parse_derive_response(raw)
        return DerivationResult(prose=prose, synthesis_statements=statements)

    def extract_ideas(
        self,
        content: str | None = None,
        source_url: str | None = None,
        *,
        reference: DocumentRef | None = None,
    ) -> list[str]:
        """Extract 3-5 key ideas, reading the source in reader mode when possible."""
        if reference is not None:
            prompt = _READER_IDEAS_PROMPT.format(
                node_id=reference.node_id,
                title=reference.title or "(no title)",
                source_url=reference.source_url or source_url or "(none)",
                content_path=reference.content_path,
                size_bytes=reference.size_bytes,
            )
            raw = self.call_llm(prompt, allow_read=True)
        else:
            if not content:
                return []
            raw = self.call_llm(
                "Extract 3-5 key ideas from this content. "
                "Return ONLY a JSON array of strings.\n\n" + content
            )
        try:
            ideas = _json.loads(raw)
            if isinstance(ideas, list):
                return [str(i) for i in ideas]
        except _json.JSONDecodeError:
            pass
        return []

    def review(
        self,
        target_content: str,
        asserting_content: str,
        edge_payload: dict,
    ) -> ReviewProposal:
        edge_context = ""
        if edge_payload:
            edge_context = f"\n\nEdge metadata:\n{_json.dumps(edge_payload, indent=2)}"

        prompt = (
            _REVIEW_SYSTEM_PROMPT
            + "\n\n"
            + "Review the impact of changing this claim.\n\n"
            + f"Target content (the node whose claim is contested):\n{target_content}\n\n"
            + f"Asserting content (the new evidence or edge):\n{asserting_content}"
            + edge_context
        )
        raw = self.call_llm(prompt)
        data = _extract_json_object(raw)
        if data is not None:
            affected_node_ids = data.get("affected_node_ids", [])
            damage_boundary_node_id = data.get("damage_boundary_node_id")
            rationale_md = data.get("rationale_md", raw)
            confidence = data.get("confidence", "low")
        else:
            affected_node_ids = []
            damage_boundary_node_id = None
            rationale_md = raw
            confidence = "low"
        return ReviewProposal(
            affected_node_ids=affected_node_ids,
            damage_boundary_node_id=damage_boundary_node_id,
            rationale_md=rationale_md,
            confidence=confidence,
        )

    @staticmethod
    def _format_reference(reference) -> str:
        docs = reference if isinstance(reference, list) else [reference]
        blocks = []
        for i, ref in enumerate(docs, start=1):
            blocks.append(
                f"Document {i}:\n"
                + _DERIVE_READER_USER_TEMPLATE.format(
                    node_id=ref.node_id,
                    title=ref.title or "(no title)",
                    source_url=ref.source_url or "(none)",
                    content_path=ref.content_path,
                    size_bytes=ref.size_bytes,
                )
            )
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # RPC process lifecycle
    # ------------------------------------------------------------------

    def _ensure_process(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return
        if self._spawn_count >= self._MAX_SPAWNS:
            raise RuntimeError(
                "omp RPC process crashed repeatedly; giving up "
                f"(last stderr: {' | '.join(self._stderr_tail[-3:]) or 'empty'})"
            )
        self._spawn()

    def _spawn(self) -> None:
        if self._proc is not None:
            self._kill()
        try:
            proc = subprocess.Popen(
                [self._cli_cmd, "--mode", "rpc", "--no-session", "--tools=read"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{type(self).__name__} requires the '{self._cli_cmd}' CLI. "
                "Install it from https://ohmy-pi.dev"
            ) from None
        self._proc = proc
        self._spawn_count += 1
        self._stderr_tail = []
        self._ready.clear()
        threading.Thread(
            target=self._stderr_reader, args=(proc,), daemon=True, name="omp-rpc-stderr"
        ).start()
        threading.Thread(
            target=self._reader, args=(proc,), daemon=True, name="omp-rpc-reader"
        ).start()
        if not self._ready.wait(self._startup_timeout):
            tail = self._stderr_tail[-3:]
            self._kill()
            raise RuntimeError(
                "omp RPC did not become ready"
                + (f" (stderr: {' | '.join(tail)})" if tail else "")
            )

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

    def dispose(self) -> None:
        """Terminate the RPC process if running. Idempotent."""
        self._kill()
        self._current_turn = None

    # ------------------------------------------------------------------
    # Wire plumbing
    # ------------------------------------------------------------------

    def _write(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise RuntimeError("omp RPC process is not running")
        with self._write_lock:
            proc.stdin.write(_json.dumps(obj) + "\n")
            proc.stdin.flush()

    def _abort(self, turn: _Turn) -> None:
        try:
            self._write({"id": f"a{next(self._seq)}", "type": "abort"})
        except Exception:  # noqa: BLE001
            pass
        if not turn.done.wait(self._abort_grace):
            self._kill()
            turn.error = "omp turn timed out (abort ignored; process killed)"
            turn.done.set()

    def _stderr_reader(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:
                self._stderr_tail.append(line.rstrip("\n"))
                if len(self._stderr_tail) > 50:
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
                        ev = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    self._handle(ev)
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
                        "omp RPC process exited mid-turn"
                        + (f" (stderr: {tail})" if tail else "")
                    )
                    turn.done.set()

    def _handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "ready":
            self._ready.set()
        elif t == "response":
            if not ev.get("success", True):
                turn = self._current_turn
                if (
                    turn is not None
                    and ev.get("id") == turn.id
                    and not turn.done.is_set()
                ):
                    turn.error = (
                        "omp RPC command failed: "
                        f"{ev.get('command')} {ev.get('error') or ''}".strip()
                    )
                    turn.done.set()
        elif t == "message_update":
            ame = ev.get("assistantMessageEvent") or {}
            if ame.get("type") == "text_delta":
                turn = self._current_turn
                if turn is not None:
                    turn.text_parts.append(ame.get("delta") or "")
        elif t in ("message_end", "turn_end"):
            # The real wire carries the stop reason on the message envelope,
            # not on agent_end (observed 2026-08-05 on omp 17.2.9).
            msg = ev.get("message") or {}
            reason = msg.get("stopReason") or ev.get("stopReason")
            if reason:
                self._last_stop_reason = reason
        elif t == "agent_end":
            turn = self._current_turn
            if turn is not None:
                turn.stop_reason = ev.get("stopReason") or self._last_stop_reason
                turn.done.set()
        elif t == "extension_ui_request":
            # Headless policy: widget frames are fire-and-forget; everything
            # else (selector/confirm/input/open_url) is answered cancelled.
            # Auth must pre-exist in ~/.omp.
            if ev.get("method") != "setWidget":
                try:
                    self._write(
                        {
                            "type": "extension_ui_response",
                            "id": ev.get("id"),
                            "cancelled": True,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
        # tool_execution_*, available_commands_update, auto_compaction_*,
        # auto_retry_*, thinking_level_changed — observed, not needed.


_LIVE: weakref.WeakSet = weakref.WeakSet()


def _dispose_all() -> None:
    for agent in list(_LIVE):
        agent.dispose()


atexit.register(_dispose_all)
