"""LLMClient interface and real Anthropic implementation.

Tests inject FakeLLMClient via MEMEX_LLM_MODULE env var.
"""
from __future__ import annotations

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


class MiniMaxLLMClient(LLMClient):
    """MiniMax-backed LLM client using the OpenAI-compatible API.

    Requires MINIMAX_API_KEY and optionally MINIMAX_BASE_URL env vars.
    Default model: MiniMax-Text-01.
    """

    def __init__(self, model: str = "MiniMax-Text-01") -> None:
        self.model = model

    def derive(self, content: str) -> DerivationResult:
        import os
        from openai import OpenAI

        client = OpenAI(
            api_key=os.environ["MINIMAX_API_KEY"],
            base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1"),
        )

        system_prompt = (
            "You are a knowledge synthesis assistant. Given source material, produce a concise "
            "summary in markdown. For any statement you infer beyond what the source directly says, "
            "prefix it with '> Synthesis:'. Keep sourced facts distinguishable from interpretation.\n\n"
            "Return a JSON object with two fields:\n"
            '  "prose": string — the full markdown summary with > Synthesis: markers\n'
            '  "synthesis_statements": array of strings — each synthesised statement (without the prefix)\n'
        )

        response = client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Summarise the following source material:\n\n{content}",
                },
            ],
        )

        import json as _json

        raw = response.choices[0].message.content or ""
        try:
            data = _json.loads(raw)
            prose = data.get("prose", raw)
            synthesis_statements = data.get("synthesis_statements", [])
        except (_json.JSONDecodeError, AttributeError, KeyError):
            prose = raw
            synthesis_statements = []

        return DerivationResult(prose=prose, synthesis_statements=synthesis_statements)


def load_llm_client(module_path: str | None = None) -> LLMClient:
    """Load an LLM client from a 'module:Class' string, or return the default AnthropicLLMClient.

    Used by the CLI to allow test injection via MEMEX_LLM_MODULE env var.
    """
    if not module_path:
        return AnthropicLLMClient()
    module_name, _, class_name = module_path.partition(":")
    import importlib
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    return cls()
