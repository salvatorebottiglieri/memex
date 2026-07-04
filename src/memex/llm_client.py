"""LLMClient interface and real Anthropic implementation.

Tests inject FakeLLMClient via MEMEX_LLM_MODULE env var.
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


class LLMClient:
    """Protocol / base class for LLM clients.

    Implementations must provide a derive(content) -> DerivationResult method
    and a review(target_content, asserting_content, edge_payload) -> ReviewProposal method.
    """

    def derive(self, content: str) -> DerivationResult:
        raise NotImplementedError

    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> ReviewProposal:
        raise NotImplementedError



class AnthropicLLMClient(LLMClient):
    """Real Anthropic-backed LLM client using structured JSON output."""

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


    def review(self, target_content: str, asserting_content: str, edge_payload: dict) -> ReviewProposal:
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


def _verify_llm_client_methods(client: object, module_path: str) -> None:
    """Verify the loaded LLM client instance has both derive and review callables.

    Raises ImportError if either method is missing or not callable.
    """
    for method_name in ("derive", "review"):
        if not hasattr(client, method_name) or not callable(getattr(client, method_name)):
            raise ImportError(
                f"LLM client '{module_path}' is missing required method '{method_name}'. "
                f"Loaded client class must implement both derive() and review()."
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
                delay = base_delay * (2 ** attempt)
                jitter = delay * random.uniform(-0.5, 0.5)
                time.sleep(delay + jitter)
    raise last_exc


def load_llm_client(module_path: str | None = None) -> LLMClient:
    """Load an LLM client from a 'module:Class' string, or return the default AnthropicLLMClient."""
    if not module_path:
        return AnthropicLLMClient()
    from memex.plugin import load_class
    client = load_class(module_path)
    _verify_llm_client_methods(client, module_path)
    return client
