# Trust-state machine gates the agent's right to stop; human review is targeted

Every node has a trust state, drawn from the following **strict ordinal** (highest to lowest):

    human-approved > auto-verified > draft > stale

A node also has a `contested` flag, orthogonal to trust state (see ADR-0012). **The agent may stop on a node during top-down navigation only if `trust_state ∈ {auto-verified, human-approved}` AND `is_contested = 0`**; otherwise it must descend.

## Cascading trust regression

The provenance DAG is a chain of trust: a node cannot have a trust state higher than the lowest parent in its provenance ancestry. When a parent's trust state regresses (moves to a lower ordinal rank), all direct children are **capped** to the new state. If a child also drops, the cascade recurses transitively through the entire downstream DAG.

Examples:

| Parent change | Child effect |
|---|---|
| `auto-verified → draft` | Child capped at `draft` |
| `human-approved → stale` | Child capped at `stale` |
| `draft → stale` | Child capped at `stale` |
| `draft → auto-verified` (upgrade) | **No cascade** — the rule is one-way conservative. Children only lose trust, never gain it automatically. Re-promotion requires explicit re-derive or human adjudication. |

This is the strictest valid policy: any regression in the source material immediately degrades everything built on it. The system never presents content as trustworthy when its foundation has been called into question.

## Targeted human review

Review time is the scarcest resource in the system; reviewing everything would kill it by attrition, so review is spent surgically on risk triggers:

- Failed deterministic check
- Detected `contradicts` edge
- Low confidence
- Tier over-claim (a node claiming a higher tier than its depth supports)

Human review is configurable per policy (which tiers, which triggers, batch/on-consultation/sampled, per-session budget).

## Deterministic checks (planned, not yet implemented)

The following checks were outlined during design but are **not yet implemented** in `checks.py`:

- Resolvable provenance for every claim (no floating claims)
- No dangling refs
- Tier/depth consistency
- Not-stale-vs-parents (partially addressed by the cascade rule above)
- `> Synthesis:` marker on non-sourced claims
- Size/scope bounds

The cascade rule (above) was added to `store.update_trust_state` and replaces the original "not-stale-vs-parents" check with a proactive invariant rather than a reactive validation.
