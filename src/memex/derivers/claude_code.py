"""Agent backed by the Claude Code CLI in stream-json mode.

``claude --output-format stream-json --verbose`` is the Claude Code SDK's
non-interactive wire: a long-lived process exchanging newline-delimited JSON
over stdio. One process is spawned per memex invocation (lazily, on the first
call), reused across the whole batch, and disposed at exit — the CLI boots
once instead of once per call.

The long-lived-process lifecycle (spawn/respawn/timeouts/dispose/atexit) lives
in :mod:`memex.derivers.subprocess` (:class:`SubprocessClient`), shared with
other wire adapters; this module supplies only the stream-json wire hooks,
prompts, and the derive/review/ideas logic.

Reader mode: the agent reads source documents itself via the Read tool, so
``derive``/``extract_ideas`` receive a :class:`DocumentRef` instead of inlined
content — sources of any length fit without a prompt cap. The wire exposes no
host tools; ``--allowedTools Read`` pre-approves the Read tool so reader mode
works, and the ``control_response`` deny is a defensive fallback for residual
control_requests (headless policy), so every result is parsed from the text
path.

The derive/review/ideas prompt bodies intentionally diverge from
:mod:`memex.derivers.pi` only in the submission instruction — omp exposes host
tools, while this wire returns the JSON envelope directly — accepted by the
ticket #121/#122 design.

Usage: ``MEMEX_AGENT=memex.derivers.claude_code:ClaudeCodeAgent``
"""

import json as _json
import os

from memex.agent import Agent
from memex.derivers.subprocess import (
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
    "4. Return your response as a JSON object with keys: 'prose' (the full markdown), "
    "'synthesis_statements' (list of strings, each without the '> Synthesis:' prefix). "
    "Return ONLY the JSON object — no markdown code fences, no commentary."
)

_DERIVE_USER_TEMPLATE = "# Source material\n\n{content}\n"

# Reader mode: the agent reads the referenced document itself in multiple
# passes (Read tool, offset/limit), so sources of any length fit without
# inlining or a prompt cap. Survey pass -> content passes -> derivation.
_DERIVE_READER_SYSTEM_PROMPT = (
    "You are a research analysis assistant. One or more source documents are "
    "available at the paths listed below. READ EVERY DOCUMENT YOURSELF, fully, in "
    "multiple passes with the Read tool, then produce a structured derivation note "
    "following these rules:\n"
    "1. Survey pass: read the beginning of each document (Read tool, offset=1) to "
    "grasp structure; use each document's size to plan the number of passes.\n"
    "2. Content passes: read each WHOLE document in chunks of ~150 lines (Read tool: "
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
    "'synthesis_statements' (list of strings, each without the '> Synthesis:' prefix). "
    "Return ONLY the JSON object — no markdown code fences, no commentary."
)

_READER_IDEAS_PROMPT = (
    "You are an ideas extractor. A source document is available at the path below. "
    "READ IT YOURSELF with the Read tool (multiple passes, offset/limit) — never "
    "other files or tools — then extract 3-5 key ideas.\n\n"
    "Return ONLY a JSON array of strings, no other text.\n\n"
    "# Source document\n\n"
    "- id: {node_id}\n"
    "- title: {title}\n"
    "- source_url: {source_url}\n"
    "- path: {content_path}\n"
    "- size_bytes: {size_bytes}\n"
)

_INLINE_IDEAS_PROMPT = (
    "Extract 3-5 key ideas from this content. Return ONLY a JSON array of "
    "strings, no other text.\n\n{content}"
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
    "Return ONLY the JSON object itself — no markdown code fences, no commentary. "
    "If you cannot determine affected nodes, use "
    '{"affected_node_ids": [], "damage_boundary_node_id": null, "rationale_md": "<explanation>", "confidence": "low"}.\n'
)


class ClaudeCodeAgent(Agent):
    """Agent powered by the Claude Code CLI in stream-json mode.

    Spawns ``claude --output-format stream-json --verbose --tools Read
    --allowedTools Read --permission-mode <mode>`` lazily on the first call
    and reuses it for the lifetime of this object (one process per memex
    invocation — the CLI boots once per batch, not once per node).

    Requires the ``claude`` CLI (https://code.claude.com) on PATH, with
    authentication already present (headless policy: no interactive login).

    Usage: ``MEMEX_AGENT=memex.derivers.claude_code:ClaudeCodeAgent``
    """

    _cli_cmd = "claude"
    can_read_files = True

    # Result subtypes that mean "the turn produced a complete answer".
    # "result" is the legacy system/result frame. Anything else —
    # error, error_max_turns, … — must surface as a failure.
    _CLEAN_RESULT_SUBTYPES = frozenset({"success", "result"})

    def __init__(
        self,
        *,
        timeout: int | None = None,
        startup_timeout: int | None = None,
    ) -> None:
        self._client = SubprocessClient(
            name="claude stream-json",
            cli_name=self._cli_cmd,
            install_hint="Install it from https://code.claude.com",
            argv=self._build_argv,
            is_ready=self._is_ready_event,
            send_prompt=self._send_prompt,
            on_event=self._on_event,
            # The wire has no abort command: timeout = SIGTERM → SIGKILL
            # (the client's default abort), and the next call respawns.
            clean_stop_reasons=self._CLEAN_RESULT_SUBTYPES,
            timeout_env="MEMEX_CC_TIMEOUT",
            startup_timeout_env="MEMEX_CC_STARTUP_TIMEOUT",
            timeout=timeout,
            startup_timeout=startup_timeout,
        )

    # ------------------------------------------------------------------
    # Agent seam
    # ------------------------------------------------------------------

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        """Run one turn against the stream-json process; return the assembled text.

        The process always runs with the Read tool (reader mode), so
        ``allow_read`` is accepted for signature compatibility with the seam
        but does not change process flags. Serializes turns: the wire carries
        one prompt at a time.

        Raises RuntimeError on timeout, crash, or any result subtype other
        than a clean one — never returns a partial turn.
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
            raw = self.call_llm(_INLINE_IDEAS_PROMPT.format(content=content))
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
        return parse_review_raw(raw)

    def dispose(self) -> None:
        """Terminate the stream-json process if running. Idempotent."""
        self._client.dispose()

    # ------------------------------------------------------------------
    # Wire hooks (bound methods passed to the SubprocessClient)
    # ------------------------------------------------------------------

    def _build_argv(self) -> list[str]:
        """Wire hook: the Claude Code stream-json command line (SDK order).

        ``--verbose`` is required: assistant events then carry the FULL
        message snapshot, so text assembly is last-wins instead of deltas.
        """
        mode = os.environ.get("MEMEX_CC_PERMISSION_MODE", "default")
        return [
            self._cli_cmd,
            "--output-format",
            "stream-json",
            "--verbose",
            "--tools",
            "Read",
            "--allowedTools",
            "Read",
            "--permission-mode",
            mode,
            "--no-session-persistence",
            "--input-format",
            "stream-json",
        ]

    def _is_ready_event(self, ev: dict) -> bool:
        """Wire hook: classify the readiness frame."""
        return ev.get("type") == "system" and ev.get("subtype") == "init"

    def _send_prompt(self, turn: Turn, prompt: str) -> None:
        """Wire hook: write the prompt frame for a turn."""
        self._client.write(
            {
                "type": "user",
                "session_id": "",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                },
            }
        )

    def _on_event(self, ev: dict, turn: Turn | None) -> None:
        """Wire hook: interpret one event frame (runs in the client's reader thread).

        ``turn`` is the client's current turn; it is None for events that
        arrive before the first prompt (e.g. readiness is classified by
        ``_is_ready_event`` and never reaches this hook).
        """
        t = ev.get("type")
        if t == "assistant":
            if turn is None:
                return
            msg = ev.get("message") or {}
            if msg.get("parent_tool_use_id"):
                # Subagent transcript, not the main turn's answer.
                return
            content = msg.get("content")
            if not isinstance(content, list) or not content:
                return
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text:
                # REPLACE, not append: --verbose carries the full accumulated
                # snapshot, so only the last non-empty one counts (last-wins).
                turn.text_parts[:] = [text]
        elif t == "result" or (t == "system" and ev.get("subtype") == "result"):
            # The legacy {"type": "system", "subtype": "result"} frame resolves
            # exactly like the modern "result" frame.
            if turn is None:
                return
            subtype = ev.get("subtype") or ""
            if subtype in self._CLEAN_RESULT_SUBTYPES and not ev.get("is_error", False):
                turn.stop_reason = subtype
                if not turn.text_parts and isinstance(ev.get("result"), str):
                    # Fallback: no assistant text — the answer rides in the
                    # double-encoded result.result field.
                    try:
                        decoded = _json.loads(ev["result"])
                        if isinstance(decoded, str):
                            turn.text_parts.append(decoded)
                        else:
                            turn.text_parts.append(ev["result"])
                    except _json.JSONDecodeError:
                        turn.text_parts.append(ev["result"])
            else:
                turn.error = (
                    f"claude stream-json turn ended with result subtype: {subtype}"
                )
            turn.done.set()
        elif t == "control_request":
            request = ev.get("request") or {}
            if request.get("subtype") == "can_use_tool":
                # Headless policy: the model may never use tools; deny.
                try:
                    self._client.write(
                        {
                            "type": "control_response",
                            "response": {
                                "subtype": "success",
                                "request_id": ev.get("request_id"),
                                "response": {
                                    "behavior": "deny",
                                    "message": "denied by memex headless policy",
                                },
                            },
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass
        # progress, rate_limit_event, user, and anything else: ignored.
