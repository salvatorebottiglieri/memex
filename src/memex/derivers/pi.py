"""Agent backed by the omp CLI in RPC mode.

``omp --mode rpc`` is the protocol the omp SDK documents for non-Node hosts:
a long-lived process exchanging newline-delimited JSON over stdio. One process
is spawned per memex invocation (lazily, on the first call), reused across the
whole batch, and disposed at exit — the engine boots once instead of once per
call (ADR-0017).

The long-lived-process lifecycle (spawn/respawn/timeouts/dispose/atexit) lives
in :mod:`memex.derivers.subprocess` (:class:`SubprocessClient`), shared with
other wire adapters; this module supplies only the omp wire hooks, host
tools, prompts, and the derive/review/ideas logic.

Reader mode: the agent reads source documents itself via the read tool, so
``derive``/``extract_ideas`` receive a :class:`DocumentRef` instead of inlined
content — sources of any length fit without a prompt cap.
"""

import itertools
import json as _json
import threading

from memex.agent import Agent
from memex.derivers.subprocess import (
    CLEAN_STOP_REASONS,
    SubprocessClient,
    Turn,
    format_reference,
    parse_ideas_raw,
    parse_review_raw,
)
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
    "4. Factual fidelity: statistics or specific numbers absent from the source "
    "material MUST be omitted or marked as a synthesis statement — never invented, "
    "rounded, or approximated from memory. In a synthesis over multiple sources, "
    "every fact taken from a source MUST be followed by an inline link "
    "[[filename|alias]] naming the parent it comes from (the # Sources block lists "
    "the exact link targets); a single-source note needs no links.\n"
    "5. Return your response as a JSON object with keys: 'prose' (the full markdown), "
    "'synthesis_statements' (list of strings, each without the '> Synthesis:' prefix).\n"
    "6. Submit the result by calling the submit_derivation tool with "
    "{prose: <the full markdown>, synthesis_statements: <the list>}. The tool call "
    "is the answer — do not print the payload as plain text. If the "
    "submit_derivation tool is unavailable, return the JSON object from rule 5 instead."
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
    "6. Factual fidelity: statistics or specific numbers absent from the source "
    "material MUST be omitted or marked as a synthesis statement — never invented, "
    "rounded, or approximated from memory. In a synthesis over multiple sources, "
    "every fact taken from a source MUST be followed by an inline link "
    "[[filename|alias]] naming the parent it comes from (the link target is the "
    "stem of the parent's listed path); a single-source note needs no links.\n"
    "7. End with a ## Synthesis section whose body is one or more bullet points, "
    "each of the form \"> Synthesis: <inference>\". There MUST be at least one "
    "such statement. The exact prefix '> Synthesis:' is required.\n"
    "8. Return your response as a JSON object with keys: 'prose' (the full markdown), "
    "'synthesis_statements' (list of strings, each without the '> Synthesis:' prefix).\n"
    "9. Submit the result by calling the submit_derivation tool with "
    "{prose: <the full markdown>, synthesis_statements: <the list>}. The tool call "
    "is the answer — do not print the payload as plain text. If the "
    "submit_derivation tool is unavailable, return the JSON object from rule 8 instead."
)

_READER_IDEAS_PROMPT = (
    "You are an ideas extractor. A source document is available at the path below. "
    "READ IT YOURSELF with the read tool (multiple passes, offset/limit) — never "
    "other files or tools — then extract 3-5 key ideas.\n\n"
    "Submit the result by calling the submit_ideas tool with "
    "{{ideas: <array of strings>}}. If the submit_ideas tool is unavailable, "
    "return ONLY a JSON array of strings, no other text.\n\n"
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
    "Submit the result by calling the submit_review tool with the four fields. "
    "If the submit_review tool is unavailable, respond with ONLY the JSON object "
    "itself — no markdown code fences, no commentary, and no other tool calls. "
    "If you cannot determine affected nodes, use "
    '{"affected_node_ids": [], "damage_boundary_node_id": null, "rationale_md": "<explanation>", "confidence": "low"}.\n'
)


def _clean_json_escapes(text: str) -> str:
    """Undo JSON string escapes a model may have emitted inside a host-tool
    payload (observed: apostrophes as ``\\'``). Only the escapes that cannot be
    intentional prose are unescaped; ``\\n``/``\\t`` are left alone."""
    return text.replace("\\'", "'")


# Host tools registered via set_host_tools (phase 2, ADR-0017). The agent calls
# them with structured arguments; the host replies host_tool_result. A captured
# payload wins over text parsing — the result arrives typed, no JSON-in-text.
# `parameters` is a JSON Schema object (the RPC wire takes it verbatim).
_HOST_TOOLS: list[dict] = [
    {
        "name": "submit_derivation",
        "label": "Submit Derivation",
        "description": (
            "Submit the final structured derivation: the full markdown prose "
            "plus the list of synthesis statements. This is the authoritative "
            "answer — do not also print the payload as plain text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prose": {"type": "string"},
                "synthesis_statements": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["prose", "synthesis_statements"],
        },
    },
    {
        "name": "submit_ideas",
        "label": "Submit Ideas",
        "description": "Submit the extracted 3-5 key ideas as a list of strings.",
        "parameters": {
            "type": "object",
            "properties": {"ideas": {"type": "array", "items": {"type": "string"}}},
            "required": ["ideas"],
        },
    },
    {
        "name": "submit_review",
        "label": "Submit Review",
        "description": (
            "Submit the review proposal: affected node ids, deepest affected "
            "node (or null), markdown rationale, and a confidence of "
            "high|medium|low."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "affected_node_ids": {"type": "array", "items": {"type": "string"}},
                "damage_boundary_node_id": {"type": ["string", "null"]},
                "rationale_md": {"type": "string"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["affected_node_ids", "rationale_md", "confidence"],
        },
    },
    {
        "name": "submit_verdicts",
        "label": "Submit Validation Verdicts",
        "description": (
            "Submit the adversarial-validation verdicts: V1 evidence checks "
            "carry a per-claim verdicts array (claim, verdict, evidence_quote "
            "for SUPPORTED; source_examined + absence_explanation for "
            "UNSUPPORTED); V2 re-elaboration carries a single passes/reason "
            "pair. This is the authoritative answer — do not also print the "
            "payload as plain text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdicts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "verdict": {
                                "type": "string",
                                "enum": ["SUPPORTED", "COMMON_KNOWLEDGE", "UNSUPPORTED"],
                            },
                            "evidence_quote": {"type": "string"},
                            "source_examined": {"type": "string"},
                            "absence_explanation": {"type": "string"},
                        },
                    },
                },
                "passes": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            # One tool, two payload shapes: V1 carries the per-claim
            # verdicts array, V2 the single passes/reason pair. Each shape
            # keeps its own `required` list (like every other host tool) so
            # an LLM omitting the payload fields is rejected by the schema
            # instead of degrading to pass-with-warning.
            "anyOf": [
                {"required": ["verdicts"]},
                {"required": ["passes"]},
            ],
        },
    },
]


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

    _ABORT_GRACE = 10

    def __init__(
        self,
        *,
        timeout: int | None = None,
        startup_timeout: int | None = None,
        abort_grace: int | None = None,
    ) -> None:
        self._abort_grace = (
            abort_grace if abort_grace is not None else self._ABORT_GRACE
        )
        self._last_stop_reason: str | None = None
        self._last_tool_payloads: dict[str, dict] = {}
        self._resp_events: dict[str, threading.Event] = {}
        self._resp_errors: dict[str, str] = {}
        self._seq = itertools.count(1)
        self._client = SubprocessClient(
            name="omp RPC",
            cli_name=self._cli_cmd,
            install_hint="Install it from https://ohmy-pi.dev",
            argv=self._build_argv,
            is_ready=self._is_ready_event,
            send_prompt=self._send_prompt,
            on_event=self._on_event,
            after_ready=self._after_ready,
            abort=self._abort,
            clean_stop_reasons=CLEAN_STOP_REASONS,
            timeout_env="MEMEX_OMP_TIMEOUT",
            startup_timeout_env="MEMEX_OMP_STARTUP_TIMEOUT",
            timeout=timeout,
            startup_timeout=startup_timeout,
        )

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
        return self._client.call_llm(prompt, allow_read=allow_read)

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
                + format_reference(reference)
            )
            raw = self.call_llm(prompt, allow_read=True)
        else:
            prompt = (
                _DERIVE_SYSTEM_PROMPT
                + "\n\n"
                + _DERIVE_USER_TEMPLATE.format(content=content or "")
            )
            raw = self.call_llm(prompt)
        payload = self.last_tool_payload("submit_derivation")
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("prose"), str)
            and isinstance(payload.get("synthesis_statements"), list)
        ):
            # The tool path does not pass through json.loads (unlike the text
            # path), so JSON escapes the model emits (observed: apostrophes as
            # \') would land verbatim in the file and drift from the DB column,
            # tripping the D2 synthesis-marker check. Unescape identically on
            # prose and statements so DB and file always agree.
            prose = _clean_json_escapes(payload["prose"])
            statements = [_clean_json_escapes(str(s)) for s in payload["synthesis_statements"]]
            return DerivationResult(prose=prose, synthesis_statements=statements)
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
                "Call the submit_ideas tool with {ideas: <array of strings>}. "
                "If the submit_ideas tool is unavailable, return ONLY a JSON "
                "array of strings.\n\n" + content
            )
        payload = self.last_tool_payload("submit_ideas")
        if isinstance(payload, dict) and isinstance(payload.get("ideas"), list):
            return [str(i) for i in payload["ideas"]]
        return parse_ideas_raw(raw)

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
        payload = self.last_tool_payload("submit_review")
        if isinstance(payload, dict) and isinstance(
            payload.get("affected_node_ids"), list
        ):
            return ReviewProposal(
                affected_node_ids=[str(i) for i in payload["affected_node_ids"]],
                damage_boundary_node_id=payload.get("damage_boundary_node_id"),
                rationale_md=payload.get("rationale_md") or raw,
                confidence=payload.get("confidence") or "low",
            )
        return parse_review_raw(raw)

    # ------------------------------------------------------------------
    # Wire hooks (bound methods passed to the SubprocessClient)
    # ------------------------------------------------------------------

    def _build_argv(self) -> list[str]:
        """Wire hook: the omp RPC-mode command line."""
        return [self._cli_cmd, "--mode", "rpc", "--no-session", "--tools=read"]

    def _is_ready_event(self, ev: dict) -> bool:
        """Wire hook: classify the readiness frame."""
        return ev.get("type") == "ready"

    def _send_prompt(self, turn: Turn, prompt: str) -> None:
        """Wire hook: write the prompt frame for a turn."""
        self._client.write({"id": turn.id, "type": "prompt", "message": prompt})

    def _after_ready(self) -> None:
        """Wire hook: register host tools and await the ack before the first prompt.

        Runs on the client's startup path right after the ready frame — once
        per spawned process, so each fresh process gets its own registration.
        Registration failure is non-fatal: text-parse fallback still works,
        and the error is recorded for diagnostics.
        """
        if not _HOST_TOOLS:
            return
        rid = f"h{next(self._seq)}"
        ev = threading.Event()
        self._resp_events[rid] = ev
        try:
            self._client.write(
                {"id": rid, "type": "set_host_tools", "tools": _HOST_TOOLS}
            )
        except Exception:  # noqa: BLE001
            self._resp_events.pop(rid, None)
            return
        ev.wait(self._client.startup_timeout)
        self._resp_events.pop(rid, None)

    def _abort(self, turn: Turn) -> None:
        """Wire hook: abort a timed-out turn.

        Sends the abort frame, waits ``abort_grace`` for the engine to end
        the turn, then kills the process if it ignores the abort.
        """
        try:
            self._client.write({"id": f"a{next(self._seq)}", "type": "abort"})
        except Exception:  # noqa: BLE001
            pass
        if not turn.done.wait(self._abort_grace):
            self._client.dispose()
            turn.error = "omp turn timed out (abort ignored; process killed)"
            turn.done.set()

    def _on_event(self, ev: dict, turn: Turn | None) -> None:
        """Wire hook: interpret one event frame (runs in the client's reader thread).

        ``turn`` is the client's current turn; it is None for events that
        arrive before the first prompt (e.g. the set_host_tools ack).
        """
        t = ev.get("type")
        if t == "response":
            rid = ev.get("id")
            if not ev.get("success", True):
                err = (
                    f"omp RPC command failed: "
                    f"{ev.get('command')} {ev.get('error') or ''}".strip()
                )
                if rid in self._resp_events:
                    self._resp_errors[rid] = err
                elif (
                    turn is not None
                    and rid == turn.id
                    and not turn.done.is_set()
                ):
                    turn.error = err
                    turn.done.set()
            if rid in self._resp_events:
                self._resp_events[rid].set()
        elif t == "host_tool_call":
            self._handle_host_tool_call(ev, turn)
        elif t == "message_update":
            ame = ev.get("assistantMessageEvent") or {}
            if ame.get("type") == "text_delta" and turn is not None:
                turn.text_parts.append(ame.get("delta") or "")
        elif t in ("message_end", "turn_end"):
            # The real wire carries the stop reason on the message envelope,
            # not on agent_end (observed 2026-08-05 on omp 17.2.9).
            msg = ev.get("message") or {}
            reason = msg.get("stopReason") or ev.get("stopReason")
            if reason:
                self._last_stop_reason = reason
        elif t == "agent_end":
            if turn is not None:
                turn.stop_reason = ev.get("stopReason") or self._last_stop_reason
                # Structured host-tool payloads ride on the completed turn;
                # publish them only when the turn finished cleanly.
                if turn.error is None and turn.stop_reason in CLEAN_STOP_REASONS:
                    self._last_tool_payloads = turn.tool_payloads
                turn.done.set()
        elif t == "extension_ui_request":
            # Headless policy: widget frames are fire-and-forget; everything
            # else (selector/confirm/input/open_url) is answered cancelled.
            # Auth must pre-exist in ~/.omp.
            if ev.get("method") != "setWidget":
                try:
                    self._client.write(
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

    def _handle_host_tool_call(self, ev: dict, turn: Turn | None) -> None:
        """Capture a host-tool call and answer it.

        The payload is stored on the current turn keyed by tool name; the
        reply is a plain success result — the agent already produced the
        payload, there is nothing to execute host-side.
        """
        name = ev.get("toolName")
        args = ev.get("arguments") or {}
        if isinstance(name, str) and isinstance(args, dict) and turn is not None:
            turn.tool_payloads[name] = args
        result = {
            "type": "host_tool_result",
            "id": ev.get("id"),
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }
        try:
            self._client.write(result)
        except Exception:  # noqa: BLE001
            pass

    def last_tool_payload(self, name: str) -> dict | None:
        """Return the structured payload the agent submitted via host tool ``name``
        on the most recently completed turn, or None. Agents without host tools
        return None — callers fall back to text parsing."""
        return self._last_tool_payloads.get(name)

    def dispose(self) -> None:
        """Terminate the RPC process if running. Idempotent."""
        self._client.dispose()
