# memex — Context

memex is a personal second brain: it ingests saved links, builds cited multi-level derivations over the raw sources, and serves them to an agent.

This file is the **glossary** — the project's ubiquitous language. Architectural decisions live in [`docs/adr/`](docs/adr/); the design overview lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Language

### Knowledge model

**Node**:
A unit of knowledge in the graph — either a raw source or a derivation.

**Raw source (L0)**:
The original ingested document, stored immutably. The bottom of every provenance chain.
_Avoid_: original, document, file

**Derivation**:
An LLM-produced node built from one or more lower nodes. Mutable and regenerable.
_Avoid_: summary, note, page (those are *kinds* of derivation, not the concept)

**Provenance edge**:
A vertical, mandatory, acyclic link from a derivation to the node(s) it was derived from. The only thing that can justify a claim.
_Avoid_: citation, reference, parent link

**Association edge**:
A lateral, optional, lower-trust link between related nodes (`related` | `contradicts` | `refines`). Never counts as support for a claim.
_Avoid_: link, see-also, relation

**Tier**:
The named, ordinal abstraction rank a derivation declares — the handle used to navigate ("the high level"). Drawn from a small fixed spine that grows only under a human gate.
_Avoid_: level, layer, rank

**Kind**:
What a derivation *is* — summary, comparison, definition, critique, open-question. Orthogonal to tier; open, emergent vocabulary.
_Avoid_: type, category

**Depth**:
The computed `max(parent depth) + 1` over the provenance DAG. An audit signal, not a navigation handle.
_Avoid_: level, tier

**Trust state**:
A node's position in `draft → auto-verified → human-approved → stale`. Gates whether the agent may stop on the node.
_Avoid_: status, validation flag

**Confidence**:
A `high | medium | low` quality signal derived from source count and contradictions. Distinct from trust state.

### Ingestion

**Capture**:
The act of saving a link by forwarding it to the inbox (Telegram Saved Messages).
_Avoid_: save, bookmark

**Inbox**:
The source-agnostic abstraction over captured items (`url + timestamp + optional note`).
_Avoid_: queue, feed

**Ingestion**:
Pulling captured items from the inbox, extracting their content, and storing L0.
_Avoid_: import, sync

**Backfill**:
The one-time ingestion of historical links from a WhatsApp chat export.

**Canonical key**:
The dedup identity of a source — a normalized URL or platform id (`youtube://<id>`).
_Avoid_: url, id, hash

**Ledger**:
The record of canonical keys already ingested. The source of truth for "what is already in / still pending."
_Avoid_: log, history

**Cursor**:
A per-source watermark of the last processed item (e.g. Telegram message id).
_Avoid_: offset, pointer

**Render step**:
The deterministic one-way projection of SQLite structure into markdown frontmatter and `[[wikilinks]]`.
_Avoid_: export, sync
