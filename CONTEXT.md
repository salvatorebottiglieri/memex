# memex — Context

memex is a personal second brain: it builds cited multi-level derivations over raw sources placed in the vault, and serves them to an agent.

This file is the **glossary** — the project's ubiquitous language. Architectural decisions live in [`docs/adr/`](docs/adr/); the design overview lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Language

### Knowledge model

**Node**:
A unit of knowledge in the graph — either a raw source or a derivation.

**Raw source (L0)**:
The original source file, placed by the user in the vault with a ``source_url`` frontmatter reference to the real source. The bottom of every provenance chain.

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
A node's position in `draft -> auto-verified -> human-approved -> stale`. Gates whether the agent may stop on the node.
_Avoid_: status, validation flag

**Confidence**:
A `high | medium | low` quality signal derived from source count and contradictions. Distinct from trust state.

    high    2+ direct provenance parents AND no incoming contradicts edges
    medium  1 direct provenance parent AND no incoming contradicts edges
    low     Any incoming contradicts edge, OR 0 parents (L0 nodes)

Synthesis nodes inherit the **minimum** confidence of their parent set (matching the trust cascade pattern).
When a ``contradicts`` edge targets a node, its confidence drops to ``low``,
and the minimum-confidence rule cascades transitively to all synthesis
descendants via ``find_provenance_descendants``.
Computed eagerly at node creation (column in the ``node`` table) and
recomputed when a ``contradicts`` edge is written. Exposed in ``show``,
``list``, and ``render`` YAML frontmatter.

**Synthesis statements**:
The structured list of inferences a derivation emits *beyond* what its
provenance parent(s) state. Persisted on the ``node`` row as a JSON column
(``synthesis_statements``) by the agent at derivation time. Independent of
the markdown body — the body may or may not render the same statements as
``> Synthesis:`` lines, but the column is the source of truth that the
deterministic check, the ``memex list --synthesis-statement`` filter, the
``memex backfill-synthesis`` migration, and the renderer's frontmatter all
marker is presentation (markdown).

_Avoid_: export, sync

### Review and contestation

**Contested**:
A node whose validity has been put in doubt by a contestation event awaiting human adjudication. Orthogonal to trust state: a node can be `contested` regardless of whether it is `draft`, `auto-verified`, `human-approved`, or `stale`. The agent may not stop on a contested node.
_Avoid_: pending, flagged, under review

**Contestation event**:
An occurrence that has caused one or more nodes to become `contested` and that requires human adjudication to resolve. Examples: a newly-written `contradicts` association edge; a future deterministic check that fails in a contestable way.
_Avoid_: incident, complaint, dispute

**Contested footprint**:
The set of nodes that a single contestation event has marked as `contested`. When the event is adjudicated, its footprint is removed; nodes remain `contested` only if other open events still cover them.
_Avoid_: scope, blast radius

**Review proposal**:
An analysis written by the review agent (LLM) that identifies the boundary of damage for a contestation event, in support of the human adjudication step.
_Avoid_: triage result, agent verdict, recommendation

**Damage boundary**:
The deepest node in the contested footprint, computed by the review agent from `affected_node_ids`. Non-unique in a DAG. Derived metadata, not a source of truth — the operational set is `affected_node_ids`.
_Avoid_: damage frontier, cut-off node

**Review queue**:
The collection of contestation events that have no `review_proposal` yet.
_Avoid_: pending list, worklist

**Adjudication**:
The human act of closing a contestation event with `accept`, `reject`, or `dismiss`. The only path out of the review queue.
_Avoid_: close, resolve, settle
### URL resolution (advisory for external agents)

**Resolve**:
The ``memex resolve <url>`` CLI command that classifies a URL and tells the external agent what it is and how to fetch it. Returns a JSON envelope with type, ingestability, and (when applicable) a direct URL. No LLM, only canonical-key matching + resolution rules.

**Resolution rule**:
A deterministic, code-registered pattern mapping a URL class (prefix, host pattern, or page structure) to a type and suggested fetching strategy (e.g. ``arxiv.org/abs/`` → PDF, ``github.com/blob`` → raw content). No LLM needed.
