# memex — Ontology & Inference Rules

This document captures memex's knowledge model as an explicit ontology: entity types,
relationship types, constraints, and inference rules. The rules are **not** aspirational —
they are extracted from the code (`store.py`, `checks.py`, `agent.py`, `cli.py`, ADRs).
Every rule here has a concrete implementation.

## 1. Entity model

### 1.1 Node

| Property | Type | Cardinality | Description |
|---|---|---|---|
| `id` | UUID (str) | 1 | Unique identifier |
| `kind` | enum | 1 | `raw_source` \| `summary` \| (open vocabulary) |
| `tier` | enum \| null | 0–1 | `raw` \| `notes` \| `synthesis` (fixed spine, ADR-0002). Null = L0 |
| `trust_state` | enum | 1 | `draft` > `auto-verified` > `human-approved` > `stale` (strict ordinal) |
| `depth` | int | 1 | Computed: `max(parent.depth) + 1`. L0 = 0 |
| `confidence` | enum | 1 | `high` > `medium` > `low`. Inherited from parents |
| `is_contested` | bool | 1 | Orthogonal to trust_state. `1` if any open `event_node_link` exists |
| `contested_at` | ISO-8601 \| null | 0–1 | When first contested |
| `content_path` | path | 1 | Filesystem path to markdown file |
| `synthesis_statements` | JSON array \| null | 0–1 | Structured list of inferences |
| `check_failures` | JSON array \| null | 0–1 | Deterministic check failures (if any) |
| `created_at` | ISO-8601 | 1 | Creation timestamp |

### 1.2 Edge

| Property | Type | Cardinality | Description |
|---|---|---|---|
| `id` | UUID (str) | 1 | Unique identifier |
| `type` | enum | 1 | `provenance` \| `association` |
| `relation` | enum | 1 | `derived_from` \| `related` \| `contradicts` \| `refines` |
| `from_node` | UUID | 1 | Source node |
| `to_node` | UUID | 1 | Target node |
| `written_by` | enum | 1 | `human` \| `llm` \| `check` \| `system` |

### 1.3 Event queue

See [Contestation & review](#6-contestation--review).

### 1.4 Source (ledger)

| Property | Type | Description |
|---|---|---|
| `node_id` | UUID | FK → node.id |
| `canonical_key` | str (UNIQUE) | Dedup identity (normalized URL or platform id) |
| `source_url` | str | Original URL |
| `title` | str \| null | Extracted title |
| `fetched_at` | ISO-8601 \| null | Last fetch timestamp |
| `failed` | int (bool) | `1` if last fetch failed |

---

## 2. Edge invariants

```
Rule E1 — Edge type partitions
  type = 'provenance'  ∧  relation = 'derived_from'  ⇒  provenance edge
  type = 'association' ∧  relation ∈ {'related','contradicts','refines'}  ⇒  association edge

Rule E2 — Provenance edges are acyclic
  There exists no directed cycle of provenance edges.
  (Enforced by computed depth monotonicity: depth strictly increases.)

Rule E3 — Provenance edges are vertical
  A provenance edge connects a derivation to the node(s) it was derived from.
  L0 nodes have no incoming provenance edges.

Rule E4 — Associations carry no evidentiary weight
  Association edges (related, contradicts, refines) never count as support for a claim.
  Only provenance edges can justify a claim.
```

Implementations:
- **E1**: DB CHECK constraint on `edge.type` and `edge.relation`
- **E2**: Not enforced at DB level; depth invariant in code preserves it
- **E4**: Enforced in arch design (ADR-0005); `confidence` formula ignores association edges as parent count

---

## 3. Confidence inference rules

See `src/memex/rules.py` for the declarative rule definitions (C1–C4).
The rules are evaluated in priority order (C4 > C3 > C2 > C1); first match wins.

```
Rule C5 — Synthesis confidence is the minimum of parents
  FOR each synthesis node S:
    S.confidence = min(P.confidence for P in provenance_parents(S))
    where min('high', 'medium', 'low') follows: high > medium > low
  (Applied on creation and recomputed on contradiction cascades.)

Rule C6 — Confidence recomputation is lazy
  Confidence is recomputed eagerly only when:
    - A 'contradicts' edge is written (cascade)
    - _backfill_confidence() runs (one-time migration)
  Other edge writes do NOT trigger recomputation.
```

Implementation: `store.compute_node_confidence()` delegates to `CONFIDENCE_RULES` in `rules.py`. C5 applied in `store._backfill_confidence()` (lines 242–283) and `store._propagate_contradiction()` (lines 483–550).

---

## 4. Trust state inference rules

```
Rule T1 — Trust state ordinal
  trust_state follows strict ordinal:
    human-approved  >  auto-verified  >  draft  >  stale

Rule T2 — Provenance trust cascade (one-way down)
  IF node P.trust_state regresses (moves to a lower ordinal)
     AND node C is a direct derivation of P (C → P via 'derived_from')
    THEN C.trust_state := min(C.trust_state, P.trust_state)
         Recursively for all provenance descendants.

Rule T3 — Upgrades never cascade
  IF node P.trust_state improves (moves to a higher ordinal)
     THEN provenance children are NOT affected.
          Re-promotion requires explicit re-derive or human adjudication.

Rule T4 — Multiple parents cap to the lowest
  IF node C has provenance parents {P1, P2, ..., Pn}
     AND min(Pi.trust_state) = T
    THEN C.trust_state ≤ T
```

| Parent change | Child effect |
|---|---|
| `auto-verified → draft` | Child capped at `draft` |
| `human-approved → stale` | Child capped at `stale` |
| `draft → stale` | Child capped at `stale` |
| Any upgrade | **No cascade** |

Implementation: `store.update_trust_state()` (lines 992–1040+).

---

## 5. Contradiction propagation rules

```
Rule X1 — Contradicts edge trigger
  Writing a 'contradicts' edge automatically opens a contestation event.
  No other relation ('derived_from', 'related', 'refines') triggers this flow.

Rule X2 — Event creation
  On 'contradicts' write, in a single transaction:
    1. Insert event_queue row with event_type='contradicts_edge_needs_review'
    2. Walk provenance DAG upward from the target (find all transitive descendants)
    3. Insert one event_node_link per descendant found
    4. For each newly-linked node with is_contested=0:
       is_contested := 1, contested_at := now
    5. Set target confidence = 'low'
    6. Recompute confidence for all descendant synthesis nodes (loop until stable)

Rule X3 — Contradiction propagation is atomic
  The entire X2 sequence shares the caller's transaction.
  If any step fails, the whole propagation rolls back (no orphan events).

Rule X4 — Multiple contradictions stack
  Multiple 'contradicts' edges targeting the same node produce multiple events.
  Each event is independent (its own proposal, its own decision).
  is_contested = 1 if ANY open event covers the node.

Rule X5 — Confidence cascade on contradicts (transitive)
  When a node gets confidence='low' due to a contradicts edge,
  all synthesis descendants recompute via Rule C5.
  Loop converges in at most D iterations (D = graph depth).

Rule X6 — No fast-path
  Every 'contradicts' edge produces a contestation event, regardless of
  authorship (written_by) or trust of the involved nodes.
  There is no scenario where a contradicts edge directly sets trust_state='stale'
  without human adjudication.
```

Implementation: `store.create_edge()` (lines 464–479), `store._propagate_contradiction()` (lines 483–550).

---

## 6. Contestation & review

```
Rule R1 — Event lifecycle
  event_queue.status ∈ {'pending', 'closed'}
  pending →  human adjudication (accept | reject | dismiss) → closed

Rule R2 — Proposal lifecycle
  review_proposal.status ∈ {'pending', 'accepted', 'rejected', 'dismissed'}
  One proposal per event (UNIQUE on event_id).

Rule R3 — Accept semantics
  IF review accept:
    1. For every node in affected_node_ids:
       trust_state := 'stale'
    2. Remove all event_node_link rows for this event
    3. For each formerly-linked node:
       IF no other open event covers it THEN is_contested := 0, contested_at := NULL

Rule R4 — Reject semantics
  IF review reject:
    1. Remove all event_node_link rows for this event
    2. For each formerly-linked node: recompute is_contested (same as R3.3)
    3. trust_state is UNTOUCHED

Rule R5 — Dismiss semantics
  Same as reject (R4) but recorded as 'dismissed' for audit trail.

Rule R6 — Review proposal structure
  review_proposal.affected_node_ids = JSON array of node IDs materially dependent
                                       on the contested claim
  review_proposal.damage_boundary_node_id = deepest affected node (or NULL)
  review_proposal.rationale_md = free-text Markdown explanation
  review_proposal.confidence ∈ {'high', 'medium', 'low'}
```

Implementation: `store.accept_proposal()` (lines 770–795), `store.reject_proposal()` (lines 800–835), `store.dismiss_proposal()` (lines 840–872), `store._close_contestation_event()` (lines 695–736). CLI: `review accept|reject|dismiss` commands.

---

## 7. Deterministic checks (draft → auto-verified gate)

See `src/memex/rules.py` for the declarative check definitions (D1–D5).
Constants `MIN_CHARS` and `MAX_CHARS` live in `rules.py`.

```
Rule D6 — Auto-verified gate
  A node passes from 'draft' to 'auto-verified' ONLY if all checks D1–D5 pass.
  Failures persist as JSON in node.check_failures.
  Trust state is set by the CLI caller (checks.py only reports failures).
```

Implementation: `checks.run_checks()` delegates to `CHECK_RULES` in `rules.py`. CLI: `_do_derive()` and `_do_synthesize()` call `update_trust_state()` with `check_failures` list.

---

## 8. Derivation structural rules

```
Rule S1 — Derivation note format
  A derivation note MUST contain, in order:
    1. Exactly one top-level heading: '# <title>'
       (title MUST NOT be 'Summary' or 'Untitled')
    2. Body prose summarising the source
    3. Terminal '## Synthesis' section

Rule S2 — Synthesis statement format (in prose)
  Every synthesis inference MUST appear as its own line:
    > Synthesis: <inference>
  No bullet, number, bold, or italic markup may precede the marker.
  The literal prefix '> Synthesis: ' (case-sensitive) is non-negotiable.

Rule S3 — Synthesis statement count
  Single-source derivation: minimum 1 statement.
  Cross-source synthesis: minimum 1 statement.

Rule S4 — Content/statements consistency
  Every string in synthesis_statements MUST appear verbatim in prose
  after '> Synthesis: '. Every marker line in prose MUST have a matching
  list entry. (Prompt-level contract; not enforced by parser.)

Rule S5 — Agent response envelope
  Agents MUST return JSON: {"prose": "<markdown>", "synthesis_statements": [...]}
  Falling back: if JSON parsing fails, the entire response is treated as prose
  and synthesis statements are recovered by regex: ^>\s*Synthesis:\s+(.+)$

Rule S6 — Adversarial validation
  IF validator is configured (MEMEX_VALIDATOR):
    validator checks that the derivation genuinely re-elaborates the source.
    Generic boilerplate ("the article discusses", "the author covers") fails.
  IF validator is absent, not callable, or throws: passes with warning.
```

Implementation: `agent._parse_derive_response()` (full function), agent system prompts, `agent.validate_derivation()`.

---

## 9. Agent retrieval constraints

```
Rule A1 — Agent stop condition
  An agent may stop on a node during top-down navigation ONLY IF:
    trust_state ∈ {'auto-verified', 'human-approved'}
    AND is_contested = 0
  (ADR-0004, enforced in architecture — no code check currently since
   graph retrieval is not yet implemented.)

Rule A2 — Contested nodes require descent
  IF is_contested = 1 OR trust_state = 'draft' OR trust_state = 'stale'
    THEN the agent MUST descend to provenance children.

Rule A3 — L0 nodes are always terminal
  Agents should never stop on L0 (raw_source) nodes — they are the bottom
  of the abstraction ladder and contain the original content, not synthesis.
  (ADR-0001: agent navigates top-down, stops as early as possible.)
```

---

## 10. Schema constraints (DB level)

```
Constraint SC1 — Node trust_state
  CHECK (trust_state IN ('draft','auto-verified','human-approved','stale'))

Constraint SC2 — Node confidence
  CHECK (confidence IN ('high','medium','low'))

Constraint SC3 — Edge type/relation
  CHECK (type IN ('provenance','association'))
  CHECK (relation IN ('derived_from','related','contradicts','refines'))

Constraint SC4 — Edge written_by
  CHECK (written_by IN ('human','llm','check','system'))

Constraint SC5 — Event queue
  CHECK (event_type IN ('contradicts_edge_needs_review'))
  CHECK (status IN ('pending','closed'))

Constraint SC6 — Review proposal
  CHECK (status IN ('pending','accepted','rejected','dismissed'))
  CHECK (confidence IN ('high','medium','low'))
  UNIQUE (event_id)

Constraint SC7 — Source canonical key
  UNIQUE (canonical_key)

Constraint SC8 — Provenance consistency (FK)
  edge.from_node → node.id
  edge.to_node   → node.id
  event_queue.edge_id        → edge.id
  event_queue.target_node_id → node.id
  event_node_link.event_id   → event_queue.id
  event_node_link.node_id    → node.id
  source.node_id             → node.id
```

---

## Appendix: Rule provenance

| Rule prefix | Where defined | Nature |
|---|---|---|
| E1–E4 | ADR-0005, `store.py` schema | Architectural + DB |
| C1–C6 | `store.py` `compute_node_confidence()`, `_propagate_contradiction()`, `_backfill_confidence()` | Code |
| T1–T4 | ADR-0014, `store.py` `update_trust_state()` | Architectural + Code |
| X1–X6 | ADR-0012, `store.py` `_propagate_contradiction()` | Architectural + Code |
| R1–R6 | ADR-0012, `store.py` `accept/reject/dismiss_proposal()`, `_close_contestation_event()` | Architectural + Code |
| D1–D6 | ADR-0011, `checks.py` `run_checks()` | Architectural + Code |
| S1–S6 | `agent.py` system prompts, `_parse_derive_response()`, `validate_derivation()` | Code |
| A1–A3 | ADR-0004, ADR-0001 | Architectural (not yet enforced in code) |
| SC1–SC8 | `store.py` `init_schema()` | DB |
