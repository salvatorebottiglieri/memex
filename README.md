# memex

A personal wiki / second brain.

**Goal:** ingest the links I dump into my own WhatsApp chat (videos, articles, blog posts I never have time to read), pre-compute and structure their knowledge, and make it easy to consult later — with rigorous source citation and knowledge organized both by **category** and by **level of abstraction**, so an agent can navigate it efficiently.

Inspired by Andrej Karpathy's personal-wiki approach and by `iusztinpaul/ai-research-os-workshop`. Obsidian is a view-only window onto the knowledge, not the engine.

> Status: **ingestion + derivation + rendering layers implemented.** Core CLI operational.

## CLI

memex exposes a JSON-only CLI (one command per operation, all output is structured).

| Command | Description |
|---------|-------------|
| `memex init --db <path> --vault <path>` | Create SQLite DB and vault directory (idempotent) |
| `memex status --db <path> --vault <path>` | Check if paths exist |
| `memex ingest --db <path> --vault <path> <url>` | Fetch URL, store L0 markdown, insert node+source rows (idempotent) |
| `memex ingest --db <path> --vault <path> --inbox <file>` | Ingest a WhatsApp `.txt` export, advancing a per-file cursor |
| `memex list --db <path> --vault <path>` | List all nodes |
| `memex list --db <path> --vault <path> --pending` | List canonical keys captured in the inbox but not yet ingested |
| `memex ingest --db <path> --vault <path> --from-inbox` | Flush all pending inbox items into the ledger (idempotent) |
| `memex show --db <path> --vault <path> <node-id>` | Show node details including L0 content, trust state, check failures |
| `memex derive --db <path> --vault <path> <node-id>` | Generate a notes-tier derivation from an L0 (LLM via `MEMEX_LLM_MODULE`) |
| `memex search --db <path> --vault <path> <query>` | Keyword search over derivation content (read-only) |
| `memex render --db <path> --vault <path>` | Project SQLite graph → YAML frontmatter + wikilinks on markdown files (slice 1: metadata + tags + aliases) |
| `memex capture --db <path> --vault <path>` | Poll Telegram Saved Messages and persist new captures to the inbox (env: `MEMEX_TELEGRAM_SOURCE`) |
| `memex review --db <path> --vault <path>` | Batch-generate review proposals for all pending contestation events |
| `memex review list --db <path> --vault <path>` | Show the review queue (pending events + proposals) |
| `memex review accept --db <path> --vault <path> <proposal-id>` | Accept a review proposal: affected nodes → stale |
| `memex review reject --db <path> --vault <path> <proposal-id>` | Reject a review proposal: close event, no trust_state change |
| `memex review dismiss --db <path> --vault <path> <proposal-id>` | Dismiss a review proposal: valid but harmless, no trust_state change |
| `memex contradict --db <path> --vault <path> <target-id> --asserted-by <node-id>` | Write a contradicts edge targeting a node, triggering contested propagation |

## Design

- **[CONTEXT.md](CONTEXT.md)** — the glossary (ubiquitous language).
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — vision, architecture map, open questions, non-goals.
- **[docs/adr/](docs/adr/)** — the architectural decisions (0001–0010) and *why* each was made.

## Develop

```bash
uv sync                       # install dependencies
uv run memex init --db /tmp/memex.db --vault /tmp/vault  # quick smoke test
uv run pytest                                                 # run the unit suite
uv run python tests/smoke_test.py                             # aggressive end-to-end smoke tests (real subprocess, 93 checks)
```

All output is JSON (AXI standard) — pipe to `jq` or your agent's tools.

## Test injection (env vars)

Tests inject fake collaborators without touching network or paying for LLM calls:

| Env var | Where | Effect |
|---|---|---|
| `MEMEX_FETCHER_MODULE` | `memex ingest` | Replaces `HttpFetcher` with a module:Class string (e.g. `tests.conftest:FakeFetcher`) |
| `MEMEX_LLM_MODULE` | `memex derive` | Replaces `AnthropicLLMClient` with a module:Class string (e.g. `tests.fake_llm_client:FakeLLMClient`) |

Both follow the `module:Class` import-string convention so the seam is a one-line change with no monkeypatching.
