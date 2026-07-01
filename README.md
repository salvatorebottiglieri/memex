# memex

A personal wiki / second brain.

**Goal:** ingest the links I dump into my own WhatsApp chat (videos, articles, blog posts I never have time to read), pre-compute and structure their knowledge, and make it easy to consult later — with rigorous source citation and knowledge organized both by **category** and by **level of abstraction**, so an agent can navigate it efficiently.

Inspired by Andrej Karpathy's personal-wiki approach and by `iusztinpaul/ai-research-os-workshop`. Obsidian is a view-only window onto the knowledge, not the engine.

> Status: **ingestion layer implemented.** Core CLI operational.

## CLI

memex exposes a JSON-only CLI (one command per operation, all output is structured).

| Command | Description |
|---------|-------------|
| `memex init --db <path> --vault <path>` | Create SQLite DB and vault directory (idempotent) |
| `memex status --db <path> --vault <path>` | Check if paths exist |
| `memex ingest --db <path> --vault <path> <url>` | Fetch URL, store L0 markdown, insert node+source rows (idempotent) |
| `memex list --db <path> --vault <path>` | List all nodes |
| `memex show --db <path> --vault <path> <node-id>` | Show node details including L0 content |

## Design

- **[CONTEXT.md](CONTEXT.md)** — the glossary (ubiquitous language).
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — vision, architecture map, open questions, non-goals.
- **[docs/adr/](docs/adr/)** — the architectural decisions (0001–0010) and *why* each was made.

## Develop

```bash
uv sync                   # install dependencies
PYTHONPATH=src memex init --db /tmp/memex.db --vault /tmp/vault  # quick smoke test
PYTHONPATH=src python3.12 -m pytest  # run tests
```

All output is JSON (AXI standard) — pipe to `jq` or your agent's tools.
