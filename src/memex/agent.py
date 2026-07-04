"""Agent interface, built-in DemoAgent, and Anthropic implementation.

The default agent (no env var) is DemoAgent — no API key needed.
Set MEMEX_AGENT to a ``module:Class`` string to load a custom agent.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field


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

    def review(
        self,
        target_content: str,
        asserting_content: str,
        edge_payload: dict,
    ) -> ReviewProposal:
        return ReviewProposal(
            affected_node_ids=["demo-affected-id"],
            damage_boundary_node_id=None,
            rationale_md="Demo review: no real analysis.",
            confidence="low",
        )


class AnthropicAgent(Agent):
    """Real Anthropic-backed agent using structured JSON output."""

    def derive(self, content: str) -> DerivationResult:
        import anthropic

        client = anthropic.Anthropic()

        system_prompt = (
            "You are a knowledge synthesis assistant. Given source material, produce a concise "
            "summary in markdown. For any statement you infer beyond what the source directly says, "
            "prefix it with '> Synthesis:'. Keep sourced facts distinguishable from interpretation.\n\n"
            "Return a JSON object with two fields:\n"
            '  "prose": string — the full markdown summary with > Synthesis: markers\n'
            '  "synthesis_statements": array of strings — each synthesised statement (without the prefix)\n'
        )

        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"Summarise the following source material:\n\n{content}",
                }
            ],
        )

        import json as _json

        raw = message.content[0].text
        try:
            data = _json.loads(raw)
            prose = data.get("prose", raw)
            synthesis_statements = data.get("synthesis_statements", [])
        except (_json.JSONDecodeError, AttributeError, KeyError):
            prose = raw
            synthesis_statements = []
        return DerivationResult(prose=prose, synthesis_statements=synthesis_statements)

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
    """Agent powered by the ``pi`` CLI coding assistant.

    Requires ``pi`` to be installed and available on PATH.
    Supports any provider/model configured in ``pi`` (e.g. Claude, GPT, Gemini).
    Uses ``pi -p --mode json --no-session --no-tools`` for non-interactive calls.
    """

    def _call_pi(self, prompt: str) -> str:
        import json as _json
        import subprocess as _sp

        try:
            proc = _sp.run(
                ["pi", "-p", "--mode", "json", "--no-session", "--no-tools"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "PiAgent requires the 'pi' CLI. Install it from https://pi.dev"
            ) from None
        except _sp.TimeoutExpired:
            raise RuntimeError("PiAgent call timed out after 120s") from None

        if proc.returncode != 0:
            raise RuntimeError(f"PiAgent call failed: {proc.stderr.strip()}")

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
        prompt = (
            "You are a knowledge synthesis assistant. Given source material, produce a concise "
            "summary in markdown. For any statement you infer beyond what the source directly says, "
            "prefix it with '> Synthesis:'. Keep sourced facts distinguishable from interpretation.\n\n"
            f"Source material:\n\n{content}"
        )
        prose = self._call_pi(prompt)
        # Extract > Synthesis: statements
        import re as _re
        statements = _re.findall(r"> Synthesis:\s*(.+)", prose)
        return DerivationResult(prose=prose, synthesis_statements=statements)

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
        raw = self._call_pi(prompt)
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


def _verify_agent_methods(client: object, module_path: str) -> None:
    """Verify the loaded agent instance has both derive and review callables.

    Raises ImportError if either method is missing or not callable.
    """
    for method_name in ("derive", "review"):
        if not hasattr(client, method_name) or not callable(getattr(client, method_name)):
            raise ImportError(
                f"Agent '{module_path}' is missing required method '{method_name}'. "
                f"Loaded agent class must implement both derive() and review()."
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
