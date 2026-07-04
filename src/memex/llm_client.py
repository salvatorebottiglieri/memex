"""Backward-compat re-export shim — use ``memex.agent`` instead.

Everything previously exported from this module now lives in ``memex.agent``:
Agent, AnthropicAgent, DemoAgent, DerivationResult, ReviewProposal,
call_with_retry, load_agent.
"""
from memex.agent import (  # noqa: F401
    Agent as LLMClient,
    AnthropicAgent as AnthropicLLMClient,
    DemoAgent,
    DerivationResult,
    ReviewProposal,
    call_with_retry,
    load_agent as load_llm_client,
)
