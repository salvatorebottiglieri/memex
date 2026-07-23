# memex

A personal wiki / second brain.

**Goal:** ingest the links I dump into my own WhatsApp chat (videos, articles, blog posts I never have time to read), pre-compute and structure their knowledge, and make it easy to consult later — with rigorous source citation and knowledge organized both by **category** and by **level of abstraction**, so an agent can navigate it efficiently.

Inspired by Andrej Karpathy's personal-wiki approach and by `iusztinpaul/ai-research-os-workshop`. Obsidian is a view-only window onto the knowledge, not the engine.

> Status: **ingestion + derivation + adversarial validation + rendering + staleness propagation + review + resolution implemented.** Core CLI operational.

## CLI

memex exposes a JSON-only CLI (one command per operation, all output is structured).

| Command | Description |
|---------|-------------|
| `memex init` | Create SQLite DB and vault directory (idempotent) |
| `memex status` | Check if paths exist |
| `memex extract <url>` | Fetch a URL and store it as URL-node + extracted-node (idempotent, replaces ingest) |
| `memex extract --inbox <file>` | Extract a WhatsApp `.txt` export, advancing a per-file cursor |
| `memex extract --from-inbox` | Flush all pending inbox items into the ledger |
| `memex list [--kind --tier --trust-state --confidence --limit --offset]` | List all nodes with optional filters |
| `memex list --pending` | List canonical keys captured in inbox but not yet ingested |
| `memex show <node-id>` | Show node details including L0 content, trust state, check failures |
| `memex extract-ideas <node-id>` | Extract 3-5 key ideas from a node (lightweight, no full derive) |
| `memex ideas [query]` | Search across extracted ideas (empty query = all ideas) |
| `memex derive <node-id>` | Generate a notes-tier derivation from an L0 (agent via `MEMEX_AGENT`) |
| `memex derive --all [--limit N]` | Batch-derive all un-derived L0 nodes (default limit: 10) |
| `memex search <query>` | Keyword search over derivations AND L0 metadata (title/URL/key) |
| `memex resolve [url]` | Resolve a URL through resolution rules (arXiv, GitHub, Wikipedia) |
| `memex resolve-agent <url>` | Resolve a URL using an external agent (Pi/Claude) with a browser |
| `memex cookies-export <domain>` | Export cookies for a domain (e.g. x.com) to use with resolve-agent |
| `memex synthesize <node-id> [<node-id> ...]` | Generate a synthesis-tier derivation from one or more nodes |
| `memex delete <node-id> [--cascade]` | Remove a node (logical delete, no file removal). Cascade removes descendants |
| `memex retry <node-id>` | Re-fetch a failed source URL |
| `memex stats` | Vault statistics dashboard (counts by kind/tier/trust/confidence, coverage) |
| `memex render` | Project SQLite graph -> YAML frontmatter + wikilinks on markdown files |
| `memex list --synthesis-statement "<substring>"` | Substring match against derivation synthesis statements |
| `memex backfill-synthesis [--dry-run]` | One-shot migration: parse `> Synthesis:` markers from existing derivation files into the structured column |
| `memex capture` | Poll Telegram Saved Messages, persist to inbox |
| `memex review` | Batch-generate review proposals for all pending contestation events |
| `memex review list` | Show the review queue (pending events + proposals) |
| `memex review accept/reject/dismiss <proposal-id>` | Adjudicate a review proposal |
| `memex contradict <target-id> --asserted-by <node-id>` | Write a contradicts edge, triggering contested propagation |
| `memex sync [--no-push] [--install-hooks]` | Commit vault state: render → git add → commit → push in one shot. `--install-hooks` writes a git post-merge hook that auto-renders after pull |

All commands accept `--db <path>` and `--vault <path>` (or set `MEMEX_DB` / `MEMEX_VAULT` env vars; CLI flags take precedence). Auto-detected defaults: Obsidian vault via `~/.obsidian`, DB at `<vault>/.memex/memex.db`.

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
| `MEMEX_FETCHER_MODULE` | `memex extract` | Replaces the default `RoutingFetcher` with a module:Class string (e.g. `tests.conftest:FakeFetcher`) |
| `MEMEX_AGENT` | `memex derive`, `extract-ideas`, `synthesize`, `review` | Replaces the default `DemoAgent` with a module:Class string (e.g. `tests.fake_llm_client:FakeAgent`, or `memex.agent:OMPAgent`). Omit to use `DemoAgent` (no API key needed, hardcoded output). |
| `MEMEX_VALIDATOR` | `memex derive`, `synthesize` | Loads a separate agent for adversarial quality validation. If unset, validation skipped (backwards compatible). Same module:Class convention. |

All three follow the `module:Class` import-string convention so the seam is a one-line change with no monkeypatching.

## Sharing between devices

The vault folder (markdown files + SQLite DB) can be shared between machines via git.

### Bootstrap

```bash
# Device A — pick a path, init, then git init
mkdir -p ~/vault
cd ~/vault
memex init
# export MEMEX_VAULT=~/vault in your shell rc so every command finds it
git init
echo -e ".obsidian/\n.cache/\n__pycache__/\n*.pyc\n*.pyo" > .gitignore
git add -A && git commit -m "init"
```

### Clone on device B

```bash
git clone <remote> ~/vault
# export MEMEX_VAULT=~/vault in your shell rc
memex init                    # idempotent — safe on existing DB
memex sync --install-hooks    # install auto-render on git pull (optional)
```

### Daily sync

```bash
# On either device — all three steps in one command:
memex sync

# On the other device — pull + auto-render (if hooks installed):
git pull                       # post-merge hook auto-runs memex render

# Without hooks:
git pull && memex render
```

`memex sync` does render → `git add -A` → `git commit -m "sync"` → `git push`.
Use `--no-push` to skip the push step (e.g. batch multiple changes before pushing).

### Git hooks (optional)

```bash
# Install a post-merge hook so every `git pull` auto-runs memex render:
memex sync --install-hooks
```


### Remote and credentials

`memex sync` calls `git push` without specifying a remote — it uses the default
remote configured in the vault repo. Set it up once:

```bash
git remote add origin <url>   # GitHub, GitLab, your own server, etc.
```

Credentials are handled entirely by git: SSH keys, `git-credential-libsecret`,
`git-credential-oauth`, tokens in `~/.netrc` — anything git supports.
`memex sync` never touches credentials, never asks for a password, never
manages tokens. If `git push` fails for auth, git's error is reported verbatim.

### `memex` must be on PATH

The git hook (`memex sync --install-hooks`) writes `exec memex render --vault …`.
It only works if `memex` is installed and reachable on PATH (e.g. via
`pip install memex` or `uv tool install memex`). During development, edit the
hook manually to point at `uv run python -m memex.cli render` instead.

See [ADR-0015](docs/adr/0015-shared-vault-git-sync.md) for rationale and tradeoffs.
