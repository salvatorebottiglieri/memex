# Framework-agnostic Python core; no LangGraph

The core is a plain, testable **Python library over SQLite** (ingest, dedup, index, staleness, deterministic checks, render) with no dependency on any LLM or agent framework. Ingestion is a **headless, scheduled pipeline** (cron) whose generative steps call the **Anthropic SDK (Claude)** with structured JSON output, so they are unit-testable with mocked responses. Consultation is an agentic loop layered on top.

## Considered Options

- **Standalone LangGraph app** — rejected here: the ingestion pipeline is a mostly-linear deterministic chain with a few LLM calls, not a cyclic multi-agent graph. Adopting LangGraph would be framework-by-default, not problem-driven. (LangGraph is fine elsewhere; this is a personal project with no such mandate.)
- **Claude Code skills as the orchestrator** (the reference's approach) — rejected as the *core*: it puts load-bearing logic in interpreted prompts, the reference's main weakness. Skills are fine as a thin consultation adapter (see ADR-0010).
- **Framework-agnostic library + headless pipeline + SDK** (chosen).

## Consequences

Load-bearing logic lives in deterministic, testable code; only genuine judgment (writing derivations, proposing tiers/edges, answering queries) is delegated to the LLM.
