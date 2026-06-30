# CLI is the canonical, harness-agnostic interface; no MCP

Consultation goes through a well-designed **CLI** over the core (AXI standards: predictable commands, structured JSON output, clear errors, token-frugal). Any harness — Pi, Claude Code, a custom agent — drives the *same* CLI through a paper-thin per-harness adapter, so switching harness leaves the core and the interface untouched.

## Considered Options

- **Claude Code skill as the interface** — rejected as canonical: locks consultation to one harness.
- **MCP server** — rejected: too much machinery for a single user, and it requires harness MCP support that a CLI does not. May be added later as an optional wrapper over the same core if a harness benefits.
- **CLI as the lowest common denominator** (chosen).

## Consequences

The agent is always a *client* of memex, never its owner; the knowledge base lives as library + CLI, and harnesses come and go. Mirrors the `chat_surface` decoupling already used in `avs_agents_lib`.
