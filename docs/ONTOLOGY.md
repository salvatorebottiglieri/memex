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
| `kind` | enum | 1 | `url` | `extracted` | `summary` | (open vocabulary) |
| `tier` | enum | null | 0–1 | `raw` | `notes` | `synthesis` (fixed spine, ADR-0002). Null = L0 |
| `trust_state` | enum | 1 | `draft` > `auto-verified` > `human-approved` > `stale` (strict ordinal) |
| `depth` | int | 1 | Computed: `max(parent.depth) + 1`. L0 = 0 |
| `confidence` | enum | 1 | `high` > `medium` > `low`. Inherited from parents |
| `is_contested` | bool | 1 | Orthogonal to trust_state. `1` if any open `event_node_link` exists |
| `contested_at` | ISO-8601 | null | 0–1 | When first contested |
| `content_path` | path | 1 | Filesystem path to markdown file |
| `synthesis_statements` | JSON array | null | 0–1 | Structured list of inferences |
| `check_failures` | JSON array | null | 0–1 | Deterministic (D1–D6) + adversarial (V1–V2) gate failures (if any) |
| `created_at` | ISO-8601 | 1 | Creation timestamp |

### 1.2 Edge

| Property | Type | Cardinality | Description |
|---|---|---|---|
| `id` | UUID (str) | 1 | Unique identifier |
| `type` | enum | 1 | `provenance` | `association` |
| `relation` | enum | 1 | `derived_from` | `related` | `contradicts` | `refines` |
| `from_node` | UUID | 1 | Source node |
| `to_node` | UUID | 1 | Target node |
| `written_by` | enum | 1 | `human` | `llm` | `check` | `system` |

### 1.3 Event queue

See [Contestation & review](#6-contestation--review).

### 1.4 Source (ledger)

| Property | Type | Description |
|---|---|---|
| `node_id` | UUID | FK → node.id |
| `canonical_key` | str (UNIQUE) | Dedup identity (normalized URL or platform id) |
| `source_url` | str | Original URL |
| `title` | str | null | Extracted title |
| `fetched_at` | ISO-8601 | null | Last fetch timestamp |
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

Implementation: `store.compute_node_confidence()` delegates to `CONFIDENCE_RULES` in `rules.py`. C5 applied in `store._backfill_confidence()` and `store._propagate_contradiction()`.

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

Implementation: `store.update_trust_state()`.

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

Implementation: `store.create_edge()`, `store._propagate_contradiction()`.

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

Implementation: `store.accept_proposal()`, `store.reject_proposal()`, `store.dismiss_proposal()`, `store._close_contestation_event()`. CLI: `review accept|reject|dismiss` commands.

---

## 7. Deterministic checks (draft → auto-verified gate)

See `src/memex/rules.py` for the declarative check definitions (D1–D6).
Constants `MIN_CHARS` and `MAX_CHARS` live in `rules.py`.

```
Rule D0 — Auto-verified gate
  A node passes from 'draft' to 'auto-verified' ONLY if all checks D1–D6 pass
  AND the validation DAG passes: V1 (grounding) → D7 (quote
  verification over V1's verdicts) → V2 (re-elaboration quality,
  consuming V1's verdicts; skipped when V1 is fatal).
  MEMEX_VALIDATION=off disables the DAG (deterministic D1–D7 never opt
  out — D7 runs over V1's verdicts and is vacuous without them).
  Failures accumulate in node.check_failures, each identifying its
  criterion (id prefix — 'D7: ', 'V1: ', 'V2: ' — or the
  consequence label for the D-checks, e.g. 'Link validity check
  failed'); the gate failures D6, D7, V1 and V2 carry two-level
  severity tags: fatal (D6, D7, V1-UNSUPPORTED) — one is
  enough → draft; quality (V2) → draft, human-promotable. The
  tag is an informational annotation guiding human review: both
  severities gate to draft (there is no separate quality_failed
  state) and draft nodes are human-promotable via the existing
  review flow.
  Invariant: auto-verified ⇒ every unadorned claim is grounded.
  Trust state is set by the CLI caller (checks.py only reports failures).
```

Implementation: `checks.run_checks()` delegates to `CHECK_RULES`; `validators.validate.run_validations()` runs the validation DAG (V1 → D7 → V2) from `VALIDATION_RULES` with the judge agent (`MEMEX_JUDGE`, default = the derive agent). services/derive.py and services/synthesize.py merge both failure lists (`merge_gate_failures`) and call `update_trust_state()`.

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

Rule S6 — Factual fidelity (prompt contract)
  Statistics or specific numbers absent from the source MUST be omitted
  or marked as synthesis statements — never invented, rounded, or
  approximated from memory.
  In syntheses, every source-derived fact MUST carry an inline wikilink
  [[filename|alias]] naming the parent it comes from.

Rule V1 — Evidence support (LLM-judged, DAG root)
  Every unadorned claim in the body is judged SUPPORTED /
  COMMON_KNOWLEDGE / UNSUPPORTED against the parent content, with an
  evidence quote. COMMON_KNOWLEDGE covers generic uncontroversial
  facts only; quantitative claims about specific entities are NEVER
  exempt. In syntheses, a source-derived fact WITHOUT a link is
  UNSUPPORTED (missing declaration); a claim WITH a link is judged
  against the linked parent only. In notes, every claim is judged
  against the single parent regardless of any inline links it
  carries (the notes exemption — a stray wikilink in note prose
  never redirects the judgment to a parent that does not exist).
  Negative-verdict contract: every UNSUPPORTED verdict cites the
  claim, the source examined, and why the source does not contain it
  (source_examined, absence_explanation). An UNSUPPORTED verdict
  lacking either field produces a deterministic contract-violation
  failure (symmetric to D7's SUPPORTED-without-quote failure). A
  verdict shortfall (a presented claim with no matching verdict,
  including an empty set) emits a warning — coverage is
  correlated per presented claim INSTANCE (whitespace-normalized
  claim text, inline link markers ignored): N identical presented
  claims need N verdicts, so a single verdict for a duplicated
  sentence leaves its twin unjudged, and an echo that drops or
  adds [[...]] markers still resolves to the presented claim.
  Duplicate verdicts or verdicts whose claim was never presented
  are coverage gaps, never a clean pass; grounding coverage is
  then incomplete, never silently clean.

Rule D7 — Evidence-quote verification (deterministic, over V1's output)
  Every evidence_quote V1 cites for a SUPPORTED verdict must appear
  in the cited source (linked parent for syntheses, single parent
  for notes). Matching is a literal substring with a
  whitespace-collapsed fallback: quote and source are compared with
  every run of whitespace collapsed to a single space (LLMs re-wrap
  line breaks; a fabricated quote differs in words, not whitespace).
  Quote not found → failure D7. This keeps
  LLM-judged evidence honest: an LLM that hallucinates a claim can
  hallucinate the supporting quote. COMMON_KNOWLEDGE is backstopped
  too: a COMMON_KNOWLEDGE verdict on a link-free synthesis claim is
  a missing declaration (a source-derived fact without an inline
  link is UNSUPPORTED) and fails deterministically. The cited
  source (and the COMMON_KNOWLEDGE link check) resolves from the
  PRESENTED claim text, not the verdict's echo: each verdict is
  correlated to the presented slice (whitespace-normalized, link
  markers ignored — LLMs routinely echo/normalize claim text,
  dropping or adding [[...]] markers); only a verdict matching no
  slice falls back to the echoed claim.

Rule V2 — Re-elaboration quality (LLM-judged, consumes V1's verdicts)
  Synthesis statements must be legitimate inferences from the body's
  facts, specific, and go beyond the source; boilerplate fails.
  Grounding is V1's job — V2 does not re-verify the facts; its prompt
  carries V1's per-claim verdicts (the body as V1 saw it).

DAG execution: waves run in ascending registry order (V1 first; D7
  verifies V1's quotes inside V1's wave; V2 declares
  depends_on=("V1",) + skip_when_fatal, so it runs after D7 and is
  SKIPPED when V1 has fatal failures — the node is draft already,
  re-derive re-runs both). The DAG is declarative: VALIDATION_RULES
  carries order/depends_on/skip_when_fatal/expects_full_verdicts;
  adding a criterion never edits run_validations.
  MEMEX_VALIDATION=off disables the DAG (deterministic checks never
  opt out). Judge call/parse failures degrade to pass-with-warning;
  V1 verdict shortfalls warn.

Rule V3 — Registry curation (process, not a criterion)
  VALIDATION_RULES has an entry/exit process: every new member adds
  ONE disjoint defect class with a declared scope (what it catches,
  what it does not), placed in the DAG; members exit when their
  defect class is subsumed. Without curation the family degrades into
  N overlapping mini-judges.
```

Implementation: agent system prompts (factual fidelity), `validators.validate.run_validations()` + `VALIDATION_RULES` in `rules.py` (V1–V2) with D7 quote verification in `validate.py`. Judge = `MEMEX_JUDGE` or the derive agent; `submit_verdicts` host tool (pi.py) carries structured verdicts.

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
  Agents should never stop on L0 nodes — the L0 is the URL root plus
  its extracted content node, the bottom of the abstraction ladder,
  carrying the original content, not synthesis. (ADR-0001: agent
  navigates top-down, stops as early as possible.)
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
| D7 | `validators/validate.py` (deterministic, over V1's verdicts) | Code |
| S1–S6 | agent system prompts, `_parse_derive_response()` | Code |
| V1–V2 | `validators/validate.py` `run_validations()`, `rules.py` `VALIDATION_RULES` (curated: one disjoint defect class per member, see Rule V3) | Code |
| A1–A3 | ADR-0004, ADR-0001 | Architectural (not yet enforced in code) |
| SC1–SC8 | `store.py` `init_schema()` | DB |
