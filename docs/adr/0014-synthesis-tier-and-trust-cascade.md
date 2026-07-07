# ADR-0014: Synthesis tier and trust cascade

The abstraction spine (ADR-0002) currently has two tiers implemented: `raw` (L0) and `notes`. A third tier — `synthesis` — was always envisioned: a cross-source derivation that synthesizes multiple notes-tier nodes into a higher-level summary. This ADR defines how synthesis works, and how trust state propagates when a parent node regresses.

## Decision

### Synthesis is a separate command

Synthesis gets its own CLI command rather than a flag on `derive`:

```
memex synthesize <node-id> [<node-id> ...]
```

Rationale: synthesis fundamentally differs from derive — multi-parent (vs single-parent), computed depth (vs hardcoded depth=1), different idempotency rule (set of parents vs single L0). Forcing both through `derive --tier synthesis` would add conditional branches to `_do_derive`, making it harder to read and test.

### Synthesis mechanics

| Property | Value |
|---|---|
| CLI | `memex synthesize <id1> <id2> ...` |
| Tier | `synthesis` |
| Kind | `summary` |
| Depth | `max(parent.depth for each parent) + 1` |
| Provenance edges | One `derived_from` edge per parent node |
| Idempotency | Checked by **unordered set** of parent IDs. `synthesize A B C` and `synthesize C A B` both produce "already_synthesized" if a synthesis from {A, B, C} already exists |
| Agent interface | Same as derive — `agent.derive(content)` receives concatenated content of all parent nodes |
| Deterministic checks | Runs `checks.run_checks` same as derive |

### Trigger model: demand-driven

Synthesis is created **on demand** — the agent (during a consultation) notices multiple related notes-tier nodes and proposes synthesis to the user. The user authorizes. This is consistent with ADR-0003 (lazy derivation creation) — synthesis is never automatically triggered.

### Trust cascade on regression

**Principle**: a node cannot have a trust state higher than its lowest parent in the provenance DAG. When a parent regresses (moves to a lower ordinal in the trust state hierarchy), all direct children are capped at the new state, recursively.

**Trust state ordinal** (highest to lowest):

```
human-approved > auto-verified > draft > stale
```

**Cascade behavior**:

| Parent change | Child effect |
|---|---|
| `auto-verified → draft` | Child capped at `draft` |
| `human-approved → stale` | Child capped at `stale` |
| `draft → stale` | Child capped at `stale` |
| Any upgrade (e.g. `draft → auto-verified`) | **No cascade** — upgrades never propagate. Re-promotion requires explicit action |

The cascade is implemented in `store.update_trust_state()` — after updating the target node, it walks outgoing provenance edges (`from_node = target`) and recurses on children that are now above the allowed ceiling.

**Edge cases**:

- **Multiple parents**: child's trust state is capped to the **lowest** parent's state. If synthesis has three parents (auto-verified, human-approved, draft) → synthesis capped at `draft`.
- **Human-approved → stale**: the most aggressive cascade. A human-approved node becomes stale if any ancestor regresses below it. The user explicitly accepted this in review — correctness over convenience.
- **Stale propagation**: `stale` is the bottom of the ordinal. Once a chain hits stale, everything downstream is stale. Recovery requires explicit re-derive or adjudication.

## Consequences

- **Positive**: Consistency — trust state is a DAG-wide invariant, not just a per-node label.
- **Positive**: Safety — the agent never shows high-trust content built on a low-trust foundation.
- **Positive**: Coverage — also covers the existing `contradicts` → contested path (ADR-0012), since contested is orthogonal and the cascade covers the trust state dimension.
- **Negative**: State loss — a human-approved node can lose its status if a distant ancestor regresses. Mitigation: the agent explains why (shows the regression path) and offers re-derive.
- **Negative**: Cascade misses partial upgrades — if parent A improves from draft to auto-verified but parent B stays draft, child stays draft. Only explicit re-synthesis can fix this.
