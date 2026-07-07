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
| Pending set (captured-but-not-ingested) | **built** | `memex list --pending` + `memex ingest --from-inbox` |
| Inbox flush (inbox → ledger) | **built** | `memex ingest --from-inbox` |
| Store deep module (CLI is thin) | **built** | `memex.store.Store` |
| Test injection via env var | **built** | `MEMEX_FETCHER_MODULE`, `MEMEX_AGENT` |
| Telegram Saved-Messages capture | **built** (slice 2: protocol + fake) | `memex capture` via `MEMEX_TELEGRAM_SOURCE` |
| Ingest from inbox (separated from capture) | **built** | `memex ingest --from-inbox` |
| Lazy derivation trigger | demand only (ADR-0003) | `memex derive`/`synthesize` on explicit action; density trigger deferred (YAGNI) |
| Render step (DB → frontmatter + wikilinks for Obsidian) | **built** (slice 1: metadata + tags + aliases) | `memex render` |
| Per-type extractors (YouTube transcript, PDF) | **designed** (ADR-0013) | Fetcher router dispatched via canonical key; `youtube-transcript-api` + `pypdf` |
| Staleness propagation (contested → triage → accept/reject/dismiss) | **built** | `memex review accept/reject/dismiss` |
| Human review queue / targeted review | **built** | `memex review` + `memex review list` |
| Edge authorship tracking | **built** | `edge.written_by` column |
| Edit round-trip (Obsidian wikilink edits back into DB) | **by design: Obsidian is view-only** | ADR-0008: SQLite owns structure, markdown owns content — unidirectional render |
| Trust cascade (child trust capped to lowest parent) | **designed** (ADR-0014) | Implemented in `store.update_trust_state` |
| Synthesis tier (cross-source derivation) | **designed** (ADR-0014) | `memex synthesize <id1> <id2> ...` |

## Map (as built)

Solid lines = implemented path; dashed = planned surface that doesn't yet write data.

```mermaid
flowchart TB
  USER((Me))

  subgraph Capture
    WA["WhatsApp export .txt<br/>(per-file cursor, not one-shot)"]
    TG["Telegram Saved Messages<br/>(ADR-0006)"]
  end

  WA -->|parse_whatsapp_export| INBOX["inbox table<br/>(url + ts + note)"]
  TG -->|parse_telegram_export| INBOX

  INBOX -->|ingest --inbox| ING["Ingestion"]
  URL["Direct URL"] -->|ingest &lt;url&gt;| ING

  ING -->|canonical_key dedup| LEDGER[("source table<br/>(ledger)")]
  ING -->|HttpFetcher| EXT["HTML extractor<br/>(regex on title + strip-tags)"]
  EXT --> L0["L0 markdown<br/>(&lt;node-id&gt;.md, immutable)"]

  ING -.capture-only path.->|"ingest --inbox currently<br/>captures AND ingests atomically"| INBOX

  ING -->|derive &lt;l0-id&gt;| DERIV["Deriver<br/>(Agent via MEMEX_AGENT)"]
  DERIV -->|"notes-tier summary,<br/>provenance edge"| DB[("SQLite<br/>node + edge")]
  DERIV -->|"writes derivation prose"| DMD["&lt;deriv-id&gt;.md"]

  DB --> CHK["Deterministic checks<br/>(checks.py)"]
  CHK -->|"pass → auto-verified<br/>fail → draft + check_failures JSON"| DB

  DB --> RENDER["memex render<br/>(renderer.py)"]
  RENDER --> MD_WIKI["Markdown with YAML frontmatter<br/>+ [[wikilinks]] for provenance<br/>- slice 1: metadata + tags + aliases<br/>- slice 2: edge wikilinks"]
  MD_WIKI --> OBS["Obsidian<br/>view-only"]

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
- [0012](adr/0012-staleness-propagation-via-contested.md) — Staleness propagation via contested state and human review
- [0013](adr/0013-fetcher-router-per-type-extractors.md) — Fetcher router with per-type content extractors
- [0014](adr/0014-synthesis-tier-and-trust-cascade.md) — Synthesis tier command and trust state cascade on parent regression
## Open questions (deferred)

- **Model choice & cost:** `AnthropicAgent` currently defaults to `claude-opus-4-5`. Switch to Sonnet for bulk derivation once cost matters; keep Opus for higher-tier synthesis. Tune when real volume arrives.
- ~~**Tier seed:** `raw` + `notes` are built; `synthesis` not yet. Let real use reveal whether more ordinal ranks are needed (gated, ADR-0002).~~ **Resolved** (ADR-0014): synthesis tier designed. Demand-driven, explicit `memex synthesize` command.
- ~~**Source-type extractors:** HTML article is built. YouTube transcript (the canonical-key mapping is already in `canonical_key.py`) and PDF are next. Tweets/X and others later.~~ **Resolved** (ADR-0013): fetcher router + per-type extractors designed.
- ~~**Edit round-trip:** if I hand-edit a wikilink in Obsidian, a reconcile step is needed (edge case).~~ **By design:** Obsidian is view-only. Render is unidirectional (ADR-0008).
- **Confidence scoring:** exact formula from source count + contradictions. (ADR-0014 covers trust state, not confidence — separate problem.)
- ~~**Staleness propagation:** invalidate-eagerly vs mark-and-regenerate-on-demand.~~ **Resolved** (ADR-0012).
- ~~**✅-reaction** Telegram confirmation: optional later enhancement (needs write scope).~~ **Deferred (YAGNI)** — zero utenti, zero bisogno percepito.
- ~~**Capture/ingest separation:** `memex ingest --inbox` does capture + ingest atomically.~~ **Deferred (YAGNI)** — Telegram già separato (ADR-0006). WhatsApp path è legacy funzionante, non spec.

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
