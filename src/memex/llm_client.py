"""LLMClient interface and real Anthropic implementation.

Tests inject FakeLLMClient via MEMEX_LLM_MODULE env var.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field


@dataclass
class DerivationResult:
    """The result of an LLM derivation."""

    prose: str  # Markdown prose with optional > Synthesis: markers
    synthesis_statements: list[str] = field(default_factory=list)


class LLMClient:
    """Protocol / base class for LLM clients.

    Implementations must provide a derive(content) -> DerivationResult method.
    """

    def derive(self, content: str) -> DerivationResult:
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
    return load_class(module_path)
