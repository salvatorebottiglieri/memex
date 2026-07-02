# memex — Architecture Overview

Design overview and rationale map. For precise term definitions see [`../CONTEXT.md`](../CONTEXT.md);
for the *why* behind each decision see the ADRs in [`adr/`](adr/).

## Vision

Today I dump interesting links into my own WhatsApp chat and never read them. memex turns that
graveyard into a compounding knowledge base. An ingestion pipeline pulls saved links, stores the
raw source immutably (L0), and an agent builds **derivations** on top at increasing levels of
abstraction. Everything is **auditable**: any derivation traces back through provenance links to
the raw source. I primarily consult the high-level derivations; an agent navigates top-down and
stops as early as it can (fewer tokens, less context pollution). A second class of links —
associative — lets the agent connect distant concepts for serendipity, without ever polluting the
citation chain.

Inspired by Karpathy's personal wiki and by `iusztinpaul/ai-research-os-workshop` (see below).
More ambitious than the reference on the knowledge model (arbitrary-depth DAG + validation states),
more conservative on scope (single user, no discovery/web-research subsystem).

## Status (what is built vs planned)

| Concern | State | Surface |
|---|---|---|
| Ingestion (URL → L0 markdown + node) | **built** | `memex ingest <url>` |
| WhatsApp inbox ingest (per-file cursor) | **built** | `memex ingest --inbox <file>` |
| Canonical-key dedup + ledger | **built** | Store: `lookup_by_canonical_key`, `source.failed` |
| Derivation (LLM → notes-tier + provenance edge) | **built** | `memex derive <l0-id>` |
| Deterministic checks (auto-verify gate) | **built** | `memex.checks.run_checks` |
| Keyword search over derivations | **built** | `memex search <query>` |
| Pending set (captured-but-not-ingested) | **built (table only)** | `memex list --pending` |
| Store deep module (CLI is thin) | **built** | `memex.store.Store` |
| Test injection via env var | **built** | `MEMEX_FETCHER_MODULE`, `MEMEX_LLM_MODULE` |
| Telegram Saved-Messages capture | planned (ADR-0006) | not implemented |
| Lazy density/demand trigger for derivations | manual only (ADR-0003) | `memex derive` is invoked explicitly |
| Render step (DB → wikilinks for Obsidian) | not started (ADR-0008) | no `memex render` |
| Per-type extractors (YouTube transcript, PDF) | HTML only | `HttpFetcher` is regex on `<title>` + strip-tags |
| Staleness propagation | not started | no `stale` trust_state writes yet |
| Human review queue / targeted review | not started (ADR-0004) | no `human-approved` transition yet |
| Edit round-trip (Obsidian wikilink edits back into DB) | not started | |
| Confidence scoring | not started | |

## Map (as built)

Solid lines = implemented path; dashed = planned surface that doesn't yet write data.

```mermaid
flowchart TB
  USER((Me))

  subgraph Capture
    WA["WhatsApp export .txt<br/>(per-file cursor, not one-shot)"]
    TG["Telegram Saved Messages<br/>planned — ADR-0006"]
  end

  WA -->|parse_whatsapp_export| INBOX["inbox table<br/>(url + ts + note)"]
  TG -.-> INBOX

  INBOX -->|ingest --inbox| ING["Ingestion"]
  URL["Direct URL"] -->|ingest &lt;url&gt;| ING

  ING -->|canonical_key dedup| LEDGER[("source table<br/>(ledger)")]
  ING -->|HttpFetcher| EXT["HTML extractor<br/>(regex on title + strip-tags)"]
  EXT --> L0["L0 markdown<br/>(&lt;node-id&gt;.md, immutable)"]

  ING -.capture-only path.->|"ingest --inbox currently<br/>captures AND ingests atomically"| INBOX

  ING -->|derive &lt;l0-id&gt;| DERIV["Deriver<br/>(LLMClient via MEMEX_LLM_MODULE)"]
  DERIV -->|"notes-tier summary,<br/>provenance edge"| DB[("SQLite<br/>node + edge")]
  DERIV -->|"writes derivation prose"| DMD["&lt;deriv-id&gt;.md"]

  DB --> CHK["Deterministic checks<br/>(checks.py)"]
  CHK -->|"pass → auto-verified<br/>fail → draft + check_failures JSON"| DB

  DB -.render step.-> RMD["Wikilinks in markdown<br/>planned — ADR-0008"]
  RMD -.-> OBS["Obsidian<br/>view-only — planned"]

  CLI[["CLI — canonical interface"]] --> DB
  CLI --> L0
  CLI --> DMD
  AGENT["Agent (Pi / Claude Code)<br/>thin per-harness adapter"] --> CLI
  USER --> AGENT
```

## Decisions (ADR index)

- [0001](adr/0001-primary-consumer-is-an-agent.md) — Primary consumer is an agent, not a human reader
- [0002](adr/0002-abstraction-tier-plus-depth.md) — Abstraction = declared named tier + computed depth, small fixed spine
- [0003](adr/0003-lazy-derivation-creation.md) — Derivations are created lazily (density/demand trigger)
- [0004](adr/0004-trust-state-gates-retrieval.md) — Trust-state machine gates the agent's stop; targeted review
- [0005](adr/0005-two-typed-edge-classes.md) — Two typed edge classes: provenance vs association
- [0006](adr/0006-telegram-capture-inbox-abstraction.md) — Telegram capture via inbox abstraction; WhatsApp dropped
- [0007](adr/0007-idempotent-nondestructive-ingestion.md) — Idempotent, non-destructive ingestion (canonical key + cursor)
- [0008](adr/0008-two-store-sqlite-markdown.md) — Two-store: SQLite owns structure, markdown owns content
- [0009](adr/0009-framework-agnostic-core-no-langgraph.md) — Framework-agnostic Python core; no LangGraph
- [0010](adr/0010-cli-canonical-interface-no-mcp.md) — CLI as canonical harness-agnostic interface; no MCP
- [0011](adr/0011-deterministic-checks-gate.md) — Deterministic Checks module + `> Synthesis:` gate

## Open questions (deferred)

- **Model choice & cost:** `AnthropicLLMClient` currently defaults to `claude-opus-4-5`. Switch to Sonnet for bulk derivation once cost matters; keep Opus for higher-tier synthesis. Tune when real volume arrives.
- **Tier seed:** `raw` + `notes` are built; `synthesis` not yet. Let real use reveal whether more ordinal ranks are needed (gated, ADR-0002).
- **Source-type extractors:** HTML article is built. YouTube transcript (the canonical-key mapping is already in `canonical_key.py`) and PDF are next. Tweets/X and others later.
- **Edit round-trip:** if I hand-edit a wikilink in Obsidian, a reconcile step is needed (edge case).
- **Staleness propagation:** invalidate-eagerly vs mark-and-regenerate-on-demand — leaning on-demand; confirm during build.
- **✅-reaction** Telegram confirmation: optional later enhancement (needs write scope).
- **Confidence scoring:** exact formula from source count + contradictions.
- **Capture/ingest conflation:** `memex ingest --inbox` currently does capture + ingest atomically, so the inbox table never has *pending* items via the CLI. The pending path exists for a future `memex capture` step that persists without ingesting — see ADR-0006/0007. Test coverage of `--pending` therefore writes to the inbox table directly.

## Reference: `iusztinpaul/ai-research-os-workshop`

**Steal:** two-axis organization (category × abstraction ladder), index-as-retrieval (no vector DB),
no-floating-claims + `> Synthesis:` marker, stable per-type URI scheme as dedup key, ≥2-source
promotion threshold, immutable-raw / mutable-wiki split, orchestrator-never-reads-raw,
query-grows-the-wiki.

**Avoid:** discovery rounds / gap-analyzer / mode-routing ceremony, multi-source-CLI sprawl,
prompt-defined load-bearing structure (we move it to code — ADR-0008/0009), pure-index scaling limits.

## Non-goals

- Web discovery / autonomous research (I ingest *already-saved* links).
- Multi-user, sharing, publishing.
- Real-time WhatsApp automation.
- MCP server, LangGraph, vector DB — unless a concrete need later proves otherwise.
