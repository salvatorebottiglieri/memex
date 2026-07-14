# Abstraction = declared named tier + computed depth, with a small fixed spine

Each derivation **declares a named semantic tier** (the stable handle I navigate by, e.g. `raw -> notes -> synthesis`), and the system **also stores the computed DAG depth** as a deterministic audit signal. The ordinal tier spine is small and fixed; finer tiers and `kinds` may emerge but enter the canonical vocabulary only through a human-approval gate.

## Considered Options

- **Computed depth only** — deterministic but meaningless to a human ("level 5" says nothing).
- **Declared tier only** — meaningful but drifts with no check.
- **Free emergent folksonomy** — flexible but proliferates into a swamp that is expensive to clean.
- **Hybrid: declared tier + computed depth, fixed spine + gated growth** (chosen).

## Consequences

"Arbitrary number of levels" is satisfied by **arbitrary DAG depth** and the open `kind` facet — *not* by an unbounded tier vocabulary. The depth/tier mismatch (e.g. a tier declared "cross-domain thesis" with a single raw parent) becomes a deterministic review trigger. The gate that admits new tiers is the same human-review gate used for derivations (see ADR-0004).
