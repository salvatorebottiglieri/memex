# memex

A personal wiki / second brain.

**Goal:** ingest the links I dump into my own WhatsApp chat (videos, articles, blog posts I never have time to read), pre-compute and structure their knowledge, and make it easy to consult later — with rigorous source citation and knowledge organized both by **category** and by **level of abstraction**, so an agent can navigate it efficiently.

Inspired by Andrej Karpathy's personal-wiki approach and by `iusztinpaul/ai-research-os-workshop`. Obsidian is a view-only window onto the knowledge, not the engine.

> Status: **ingestion + derivation + rendering + staleness propagation + review layers implemented.** Core CLI operational.

## CLI

memex exposes a JSON-only CLI (one command per operation, all output is structured).

| Command | Description |
|---------|-------------|
| `memex init` | Create SQLite DB and vault directory (idempotent) |
| `memex status` | Check if paths exist |
| `memex ingest <url>` | Fetch URL, store L0 markdown, insert node+source rows (idempotent) |
| `memex ingest --inbox <file>` | Ingest a WhatsApp `.txt` export, advancing a per-file cursor |
| `memex ingest --from-inbox` | Flush all pending inbox items into the ledger |
| `memex list [--kind --tier --trust-state --confidence --limit --offset]` | List all nodes with optional filters |
| `memex list --pending` | List canonical keys captured in inbox but not yet ingested |
| `memex show <node-id>` | Show node details including L0 content, trust state, check failures |
| `memex extract <node-id>` | Extract 3-5 key ideas from a node (lightweight, no full derive) |
| `memex ideas [query]` | Search across extracted ideas (empty query = all ideas) |
| `memex derive <node-id>` | Generate a notes-tier derivation from an L0 (agent via `MEMEX_AGENT`) |
| `memex derive --all [--limit N]` | Batch-derive all un-derived L0 nodes (default limit: 10) |
| `memex search <query>` | Keyword search over derivations AND L0 metadata (title/URL/key) |
| `memex synthesize <node-id> [<node-id> ...]` | Generate a synthesis-tier derivation from one or more nodes |
| `memex delete <node-id> [--cascade]` | Remove a node (logical delete, no file removal). Cascade removes descendants |
| `memex retry <node-id>` | Re-fetch a failed source URL |
| `memex stats` | Vault statistics dashboard (counts by kind/tier/trust/confidence, coverage) |
| `memex render` | Project SQLite graph -> YAML frontmatter + wikilinks on markdown files |
| `memex capture` | Poll Telegram Saved Messages, persist to inbox |
| `memex review` | Batch-generate review proposals for all pending contestation events |
| `memex review list` | Show the review queue (pending events + proposals) |
| `memex review accept/reject/dismiss <proposal-id>` | Adjudicate a review proposal |
| `memex contradict <target-id> --asserted-by <node-id>` | Write a contradicts edge, triggering contested propagation |

All commands accept `--db <path>` and `--vault <path>` (auto-detected defaults).

## Design

- **[CONTEXT.md](CONTEXT.md)** — the glossary (ubiquitous language).
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — vision, architecture map, open questions, non-goals.
- **[docs/adr/](docs/adr/)** — the architectural decisions (0001–0012) and *why* each was made.

## Develop

```bash
uv sync                       # install dependencies
uv run memex init --db /tmp/memex.db --vault /tmp/vault  # quick smoke test
uv run pytest                                                 # run the unit suite
uv run python tests/smoke_test.py                             # aggressive end-to-end smoke tests (real subprocess, 189 checks)

All output is JSON (AXI standard) — pipe to `jq` or your agent's tools.

## Test injection (env vars)

Tests inject fake collaborators without touching network or paying for LLM calls:

| Env var | Where | Effect |
|---|---|---|
| `MEMEX_FETCHER_MODULE` | `memex ingest` | Replaces the default `RoutingFetcher` with a module:Class string (e.g. `tests.conftest:FakeFetcher`) |
| `MEMEX_AGENT` | `memex derive`, `extract`, `synthesize`, `review` | Replaces the default `DemoAgent` with a module:Class string (e.g. `tests.fake_llm_client:FakeAgent`, or `memex.agent:OMPAgent`). Omit to use `DemoAgent` (no API key needed, hardcoded output). |

Both follow the `module:Class` import-string convention so the seam is a one-line change with no monkeypatching.
