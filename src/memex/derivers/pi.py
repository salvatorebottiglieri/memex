"""Derivers backed by Pi / OMP CLI tools."""

import json as _json
import re as _re

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


class PiAgent(Agent):
    """Agent powered by Pi (``@earendil-works/pi-coding-agent``).

    Uses the ``pi`` CLI under the hood (``pi -p --mode json --no-session``).

    Reader mode: the agent reads source documents itself via the read tool
    (``--tools=read``), so ``derive``/``extract_ideas`` receive a
    :class:`DocumentRef` instead of inlined content — sources of any length
    fit without a prompt cap.

    Requires ``pi`` to be installed and available on PATH.
    Supports any provider/model configured in ``pi`` (e.g. Claude, GPT, Gemini, DeepSeek).
    """

    _cli_cmd = "pi"
    can_read_files = True

    def _cli_tool_flag(self, allow_read: bool) -> list[str]:
        return ["--tools=read"] if allow_read else ["--no-tools"]

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        import subprocess as _sp

        try:
            proc = _sp.run(
                [self._cli_cmd, "-p", "--mode", "json", "--no-session"]
                + self._cli_tool_flag(allow_read),
                input=prompt,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{type(self).__name__} requires the '{self._cli_cmd}' CLI. "
                f"Install it from https://{self._cli_cmd}.dev"
            ) from None
        except _sp.TimeoutExpired:
            raise RuntimeError(f"{type(self).__name__} call timed out after 300s") from None

        if proc.returncode != 0:
            raise RuntimeError(f"{type(self).__name__} call failed: {proc.stderr.strip()}")

        # Parse JSON lines output — extract text from the last message_end
        last_text = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                msg = event.get("message", {})
                content = msg.get("content", [])
                for part in content:
                    if part.get("type") == "text":
                        last_text = part.get("text", "")
        return last_text

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
        import json as _json

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


class OMPAgent(PiAgent):
    """Agent powered by OMP (Oh My Pi — ``@nicedoc/oh-my-pi``).

    Uses the ``omp`` CLI under the hood (same interface as Pi).

    Requires ``omp`` to be installed and available on PATH.
    Supports any provider/model configured in ``omp`` (e.g. Claude, GPT, Gemini, DeepSeek).

    Usage: ``MEMEX_AGENT=memex.derivers.pi:OMPAgent``
    """

    _cli_cmd = "omp"

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        import subprocess as _sp

        try:
            proc = _sp.run(
                [self._cli_cmd, "-p", "--mode", "json", "--no-session"]
                + self._cli_tool_flag(allow_read),
                input=prompt,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{type(self).__name__} requires the '{self._cli_cmd}' CLI. "
                f"Install it from https://ohmy-pi.dev"
            ) from None
        except _sp.TimeoutExpired:
            raise RuntimeError(f"{type(self).__name__} call timed out after 300s") from None

        if proc.returncode != 0:
            raise RuntimeError(f"{type(self).__name__} call failed: {proc.stderr.strip()}")
        # Parse JSON lines output — extract text from the last message_end
        last_text = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                msg = event.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            last_text = part.get("text", "")
        return last_text
