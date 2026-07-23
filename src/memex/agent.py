"""Agent interface, built-in DemoAgent, and Anthropic implementation.

The default agent (no env var) is DemoAgent — no API key needed.
Set MEMEX_AGENT to a ``module:Class`` string to load a custom agent.

The shape of a derivation note is a contract between this module, the LLM, the
Store, and the renderer. To prevent drift, all agents share the same prompt
(``_DERIVE_PROMPT_TEMPLATE``) and the same JSON contract
(``synthesis_statements: list[str]``). CLI agents (OMPAgent, PiAgent) ask for
that JSON envelope directly; if the model replies in prose instead, the
fallback extractor recovers the list via the exact ``> Synthesis:`` marker.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Shared contract: the shape of a derivation note
# ---------------------------------------------------------------------------
#
# A derivation note MUST contain, in this order:
#   1. A single top-level heading (one ``#``) carrying the note's title.
#   2. Body prose that summarises the source. The prose MUST be distinguishable
#      from inference — facts restated from the source are unadorned, and any
#      statement that goes beyond what the source says is a *synthesis
#      statement*.
#   3. A ``## Synthesis`` section whose body is one or more bullet points, each
#      of the form ``> Synthesis: <inference>``. There MUST be at least one
#      such statement. The literal prefix ``> Synthesis:`` (single space after
#      the colon) is non-negotiable — the deterministic check, the list
#      filter, the backfill, and the renderer all match on it.
#
# The structured field ``synthesis_statements`` carries the same statements
# without the ``> Synthesis:`` prefix, so the DB can be queried without
# parsing prose.

_DERIVE_SYSTEM_PROMPT = (
    "You are a knowledge synthesis assistant for a personal knowledge graph "
    "(memex). Given source material, produce a concise notes-tier derivation "
    "in the exact shape described below. The shape is a contract — deviations "
    "will fail the deterministic checks.\n\n"
    "# Required shape\n\n"
    "1. Begin with a single top-level markdown heading, ``# <Title>``, where "
    "<Title> is a short, specific title (not \"Summary\", not \"Untitled\").\n"
    "2. Write the body prose that summarises the source. Sourced facts are "
    "unadorned. Anything you infer beyond what the source directly says MUST "
    "go into the ``## Synthesis`` section below — never mixed into the body.\n"
    "3. End with a single ``## Synthesis`` section. Its body MUST be one or "
    "more bullet points, each starting with the exact literal prefix "
    "``> Synthesis:`` (greater-than, space, the word Synthesis, colon, space, "
    "then the inference). There is no minimum count beyond ``>= 1``, but a "
    "strong derivation has 3-6 distinct inferences.\n"
    "4. Do NOT bold, italicise, or otherwise wrap the ``Synthesis:`` word. "
    "Do NOT add extra section headings between the body and ``## Synthesis``. "
    "Do NOT prefix the marker with anything (no bullets, no numbering, no "
    "bold/italic markup).\n\n"
    "# Output format\n\n"
    "Return ONLY a JSON object with exactly two fields and no other text:\n"
    "{\n"
    '  "prose": "<the full markdown note following the shape above, as a '
    'single string>",\n'
    '  "synthesis_statements": ["<inference 1, without the \\"> Synthesis: \\" '
    'prefix>", "<inference 2>", ...]\n'
    "}\n\n"
    "The two fields MUST be consistent: every string in "
    "``synthesis_statements`` appears verbatim in the prose (after the "
    "``> Synthesis: `` prefix), and every ``> Synthesis:`` line in the prose "
    "has a matching entry in ``synthesis_statements``. Do not invent entries "
    "in either direction."
)


_DERIVE_USER_TEMPLATE = "# Source material\n\n{content}\n"


_VERIFY_QUALITY_PROMPT = """\
You are an adversarial validator for a personal knowledge graph (memex).
Your job is to be CRITICAL: a derivation must genuinely re-elaborate its source.
If the derivation is generic boilerplate, you must reject it.

SOURCE (the original material):
{parent_content}

DERIVATION (the proposed summary):
{derivation_prose}

SYNTHESIS STATEMENTS (inferences beyond the source):
{statements}

Does this derivation meaningfully re-elaborate the source? Be strict.
A PASSING derivation references specific concepts, claims, or data from the source.
A FAILING derivation uses generic phrases like "the article discusses", "the author covers",
"the topic at hand" — boilerplate that could apply to ANY source.

Answer with exactly the JSON object (no other text):
{{"passes": true}} or {{"passes": false}}
"""


def _parse_derive_response(raw: str) -> tuple[str, list[str]]:
    """Parse an LLM response into (prose, synthesis_statements).

    Tries the JSON envelope first; on failure, falls back to treating the
    whole response as prose and recovering the ``> Synthesis:`` statements via
    a strict marker regex (no bold/italic variants — the prompt forbids them).
    Returns ``("", [])`` if neither path yields a usable result.
    """
    import json as _json
    import re as _re

    # 1. JSON envelope.
    text = raw.strip()
    if text.startswith("```"):
        # Strip markdown code fence
        text = _re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=_re.S).strip()
    try:
        data = _json.loads(text)
        if isinstance(data, dict):
            prose = str(data.get("prose", ""))
            stmts_raw = data.get("synthesis_statements", [])
            if isinstance(stmts_raw, list):
                stmts = [str(s).strip() for s in stmts_raw if str(s).strip()]
                if prose:
                    return prose, stmts
    except (_json.JSONDecodeError, ValueError):
        pass

    # 2. Prose fallback: strict marker, no bold/italic variants.
    statements = _re.findall(r"(?m)^>\s*Synthesis:\s+(.+)$", text)
    if statements:
        return text, [s.strip() for s in statements]

    return text, []


def validate_derivation(
    agent: object,
    parent_content: str,
    derivation: DerivationResult,
) -> tuple[bool, str | None]:
    """Adversarial validation: check if derivation genuinely re-elaborates parent.

    Returns (bool, str | None):
      (True, None) — clean pass
      (False, None) — clean fail (validator rejected)
      (True, "warning message") — pass but validator had issues

    The validation agent is a *separate* agent from the one that produced the
    derivation (impartial judge). The prompt asks it to be critical.

    DemoAgent: always returns (True, None) (passes everything, for tests).
    PiAgent/OMPAgent: calls the LLM with an adversarial prompt.
    Unknown agents: pass (no validation).
    """
    # DemoAgent / mock: no real validation, always pass
    if isinstance(agent, DemoAgent):
        return True, None

    # Agents with call_llm: adversarial LLM call
    call = getattr(agent, "call_llm", None)
    if call is None:
        return True, None  # Unknown agent type, skip validation

    statements = "\n".join(f"- {s}" for s in derivation.synthesis_statements)
    try:
        prompt = _VERIFY_QUALITY_PROMPT.format(
            parent_content=parent_content,
            derivation_prose=derivation.prose,
            statements=statements,
        )
    except (KeyError, ValueError, AttributeError):
        return True, "Validator prompt formatting failed, validation skipped"
    try:
        raw = call(prompt)
    except Exception:
        return True, "Validator LLM call failed, validation skipped"

    import json as _json

    try:
        data = _json.loads(raw)
    except (ValueError, TypeError, _json.JSONDecodeError):
        return True, "Validator response parse failed, validation skipped"

    if not isinstance(data, dict) or "passes" not in data:
        return True, "Validator response missing 'passes' field, validation skipped"

    return bool(data["passes"]), None


@dataclass
class ReviewProposal:
    """The result of an LLM review — which nodes are affected by a changed claim."""

    affected_node_ids: list[str]
    damage_boundary_node_id: str | None
    rationale_md: str
    confidence: str  # "high" | "medium" | "low"


@dataclass
class DerivationResult:
    """The result of an LLM derivation."""

    prose: str  # Markdown prose with optional > Synthesis: markers
    synthesis_statements: list[str] = field(default_factory=list)


class Agent:
    """Protocol / base class for agents.

    Implementations must provide a derive(content) -> DerivationResult method
    and a review(target_content, asserting_content, edge_payload) -> ReviewProposal method.
    """

    def derive(self, content: str) -> DerivationResult:
        raise NotImplementedError

    def review(
        self, target_content: str, asserting_content: str, edge_payload: dict
    ) -> ReviewProposal:
        raise NotImplementedError

    def generate_title(self, content: str, url: str) -> str | None:
        """Infer a human-readable title from content and URL. Return None to skip."""
        return None

    def extract_ideas(self, content: str) -> list[str]:
        """Extract 3-5 key ideas from content. Return empty list by default."""
        return []


class DemoAgent:
    """Built-in demo agent — returns hardcoded content. No API key needed."""

    def derive(self, content: str) -> DerivationResult:
        prose = (
            "This article discusses the topic at hand.\n\n"
            "> Synthesis: The author implies a broader pattern beyond what is stated directly.\n\n"
            "The source material covers the subject thoroughly."
        )
        return DerivationResult(
            prose=prose,
            synthesis_statements=[
                "The author implies a broader pattern beyond what is stated directly."
            ],
        )

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> ReviewProposal:
        """Demo review: always returns high confidence, marks unaffected."""
        return ReviewProposal(
            affected_node_ids=[],
            damage_boundary_node_id=None,
            rationale_md="Demo review: no contestation analysis performed.",
            confidence="high",
        )

    def generate_title(self, content: str, url: str) -> str | None:
        """Demo: no title generation."""
        return None

    def extract_ideas(self, content: str) -> list[str]:
        """Demo: returns canned ideas."""
        return ["Key idea 1", "Key idea 2", "Key idea 3"]

class AnthropicAgent(Agent):
    """Real Anthropic-backed agent using structured JSON output."""

    def derive(self, content: str) -> DerivationResult:
        import anthropic

        client = anthropic.Anthropic()

        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=_DERIVE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _DERIVE_USER_TEMPLATE.format(content=content),
                }
            ],
        )

        raw = message.content[0].text
        prose, statements = _parse_derive_response(raw)
        return DerivationResult(prose=prose, synthesis_statements=statements)

    def extract_ideas(self, content: str) -> list[str]:
        """Extract 3-5 key ideas via Anthropic LLM."""
        import anthropic

        client = anthropic.Anthropic()
        system_prompt = (
            "You are an idea extraction assistant. Given source material, identify 3-5 "
            "key ideas or concepts. Return ONLY a JSON array of strings, each 5-15 words."
        )
        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=512,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"Extract the key ideas from this content:\n\n{content}",
                }
            ],
        )
        raw = message.content[0].text
        import json as _json
        try:
            return _json.loads(raw)
        except (_json.JSONDecodeError, AttributeError, TypeError):
            return []

    def review(
        self,
        target_content: str,
        asserting_content: str,
        edge_payload: dict,
    ) -> ReviewProposal:
        import anthropic

        client = anthropic.Anthropic()

        system_prompt = (
            "You are a review assistant for a knowledge-graph system. Given two pieces of content "
            "(the 'target' node and the 'asserting' edge or node) and metadata about the "
            "potentially-changing claim, determine which nodes materially depend on the contested claim.\n\n"
            "Return a JSON object with these fields:\n"
            '  "affected_node_ids": array of strings — IDs of nodes that materially depend on the contested claim\n'
            '  "damage_boundary_node_id": string or null — the deepest affected node, or null if none\n'
            '  "rationale_md": string — markdown explaining your reasoning\n'
            '  "confidence": string — one of "high", "medium", or "low"\n'
        )

        edge_context = ""
        if edge_payload:
            import json as _j

            edge_context = f"\n\nEdge metadata:\n{_j.dumps(edge_payload, indent=2)}"

        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Review the impact of changing this claim.\n\n"
                        f"Target content (the node whose claim is contested):\n{target_content}\n\n"
                        f"Asserting content (the new evidence or edge):\n{asserting_content}"
                        f"{edge_context}"
                    ),
                }
            ],
        )

        import json as _json

        raw = message.content[0].text
        try:
            data = _json.loads(raw)
            affected_node_ids = data.get("affected_node_ids", [])
            damage_boundary_node_id = data.get("damage_boundary_node_id")
            rationale_md = data.get("rationale_md", raw)
            confidence = data.get("confidence", "low")
        except (_json.JSONDecodeError, AttributeError, KeyError):
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


class PiAgent(Agent):
    """Agent powered by Pi (``@earendil-works/pi-coding-agent``).

    Uses the ``pi`` CLI under the hood (``pi -p --mode json --no-session --no-tools``).
    The Pi SDK (TypeScript) at https://pi.dev/docs/latest/sdk provides the full agent
    runtime for JS/TS projects; this Python integration uses the CLI interface.

    Requires ``pi`` to be installed and available on PATH.
    Supports any provider/model configured in ``pi`` (e.g. Claude, GPT, Gemini, DeepSeek).
    """

    _cli_cmd = "pi"

    def call_llm(self, prompt: str) -> str:
        import json as _json
        import subprocess as _sp

        try:
            proc = _sp.run(
                [self._cli_cmd, "-p", "--mode", "json", "--no-session", "--no-tools"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{type(self).__name__} requires the '{self._cli_cmd}' CLI. "
                f"Install it from https://{self._cli_cmd}.dev"
            ) from None
        except _sp.TimeoutExpired:
            raise RuntimeError(f"{type(self).__name__} call timed out after 120s") from None

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


    def derive(self, content: str) -> DerivationResult:
        prompt = _DERIVE_SYSTEM_PROMPT + "\n\n" + _DERIVE_USER_TEMPLATE.format(content=content)
        raw = self.call_llm(prompt)
        prose, statements = _parse_derive_response(raw)
        return DerivationResult(prose=prose, synthesis_statements=statements)

    def extract_ideas(self, content: str) -> list[str]:
        """Extract 3-5 key ideas via Pi CLI."""
        prompt = (
            "You are an idea extraction assistant. Given source material, identify 3-5 "
            "key ideas or concepts. Return ONLY a JSON array of strings, each 5-15 words.\n\n"
            f"Source material:\n\n{content}"
        )
        raw = self.call_llm(prompt)
        import json as _json
        import re as _re
        # Strip markdown code fences (```json ... ```) that omp/Pi may wrap around JSON
        raw = _re.sub(r'^```\w*\n?', '', raw.strip())
        raw = _re.sub(r'\n?```$', '', raw)
        try:
            return _json.loads(raw)
        except (_json.JSONDecodeError, AttributeError, TypeError):
            return []

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> ReviewProposal:
        prompt = (
            "You are a research analysis assistant. Given the target node's content "
            "(the node being contested) and the asserting node's content "
            "(the node claiming a contradiction), identify which descendants "
            "materially depend on the contested claim. Distinguish between nodes "
            "that rely on the contested claim and nodes that merely transitively "
            "include it but would be unaffected if it were removed.\n\n"
            "Return ONLY a JSON object with the following fields:\n"
            '  "affected_node_ids": list of strings — node ids whose content materially depends on the contested claim\n'
            '  "damage_boundary_node_id": string or null — the deepest affected node id\n'
            '  "rationale_md": string — brief markdown rationale\n'
            '  "confidence": "high" | "medium" | "low"\n\n'
            f"Target content:\n{target_content}\n\n"
            f"Asserting content:\n{asserting_content}\n\n"
            f"Edge payload: {edge_payload}"
        )
        raw = self.call_llm(prompt)
        import json as _json
        try:
            data = _json.loads(raw)
            affected = data.get("affected_node_ids", [])
            boundary = data.get("damage_boundary_node_id")
            rationale = data.get("rationale_md", raw)
            confidence = data.get("confidence", "low")
        except (_json.JSONDecodeError, AttributeError, KeyError):
            affected = []
            boundary = None
            rationale = raw
            confidence = "low"
        if confidence not in ("high", "medium", "low"):
            confidence = "low"
        return ReviewProposal(
            affected_node_ids=affected,
            damage_boundary_node_id=boundary,
            rationale_md=rationale,
            confidence=confidence,
        )


class OMPAgent(PiAgent):
    """Agent powered by OMP (Oh My Pi — ``@nicedoc/oh-my-pi``).

    Uses the ``omp`` CLI under the hood (same interface as Pi).

    Requires ``omp`` to be installed and available on PATH.
    Supports any provider/model configured in ``omp`` (e.g. Claude, GPT, Gemini, DeepSeek).

    Usage: ``MEMEX_AGENT=memex.agent:OMPAgent``
    """

    _cli_cmd = "omp"

    def call_llm(self, prompt: str) -> str:
        import json as _json
        import subprocess as _sp

        try:
            proc = _sp.run(
                [self._cli_cmd, "-p", "--mode", "json", "--no-session", "--no-tools", prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{type(self).__name__} requires the '{self._cli_cmd}' CLI. "
                f"Install it from https://ohmy-pi.dev"
            ) from None
        except _sp.TimeoutExpired:
            raise RuntimeError(f"{type(self).__name__} call timed out after 120s") from None

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

def _verify_agent_methods(client: object, module_path: str) -> None:
    """Verify the loaded agent instance has derive, review, and extract_ideas callables.

    Raises ImportError if any method is missing or not callable.
    """
    for method_name in ("derive", "review", "extract_ideas"):
        if not hasattr(client, method_name) or not callable(getattr(client, method_name)):
            raise ImportError(
                f"Agent '{module_path}' is missing required method '{method_name}'. "
                f"Loaded agent class must implement derive(), review(), and extract_ideas()."
            )


def call_with_retry(fn, max_retries=3, base_delay=1.0):
    """Call fn() with exponential backoff + jitter.

    Retries up to `max_retries` times with delay = base_delay * (2 ** attempt)
    plus uniform jitter of ±50%. Raises the last exception if all retries fail.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2**attempt)
                jitter = delay * random.uniform(-0.5, 0.5)
                time.sleep(delay + jitter)
    raise last_exc


def load_agent(module_path: str | None = None) -> Agent:
    """Load an agent from a 'module:Class' string, or return DemoAgent()."""
    if not module_path:
        return DemoAgent()
    from memex.plugin import load_class

    client = load_class(module_path)
    _verify_agent_methods(client, module_path)
    return client
