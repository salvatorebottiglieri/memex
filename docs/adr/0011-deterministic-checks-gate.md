# Deterministic Checks module gates the draft -> auto-verified transition

Every derivation that comes out of `memex derive` is run through a fixed set of **deterministic checks** before its trust state is decided: if all checks pass, the node is promoted to `auto-verified`; if any fail, it stays in `draft` and the failure list is persisted as `node.check_failures` JSON. The Checks module is the cheap end of the trust-state machine that ADR-0004 describes.

## What gets checked

The current `memex.checks.run_checks` runs four gates (all pure, no LLM, no network, no randomness):

1. **Resolvable provenance** — the derivation has a `derived_from` edge pointing to an existing node.
2. **No dangling reference** — that target is `kind=raw_source` (an L0), not another derivation.
3. **`> Synthesis:` marker present** — any synthesised (non-sourced) claim is visibly marked so the agent can distinguish source-quote from inference.
4. **Size / scope bounds** — derivation is ≥ MIN_CHARS (100) and ≤ MAX_CHARS (50_000).

ADR-0004 lists additional gates that are *not yet implemented*: tier/depth consistency, not-stale-vs-parents. They belong here when built — adding a gate is a single function returning strings into the failure list.

## Consequences

- **Pure / deterministic** — checks have no side effects, no LLM, no network, so they are safe to re-run on demand and trivial to test (the entire test suite for `checks.py` is in-process and fast).
- **Failures are persistent** — stored as JSON on the node row, surfaced by `memex show`, and emitted in the `derive` response. The agent doesn't have to re-run checks to know why a node is draft.
- **Promote-only** — checks can move a derivation from `draft` to `auto-verified`, but never the reverse and never up to `human-approved`. Human approval (ADR-0004) is its own transition.
- **The `> Synthesis:` marker is a code-level rule, not a prompt convention** — even though the LLM is asked to produce it via the system prompt, the check enforces it. The prompt can drift; the check can't.
- **Scope** — this ADR covers the gate that moves a derivation from `draft` to `auto-verified`. It does not cover the review agent that produces `review_proposal`s, nor the propagation of `contested` state: those live in ADR-0012.

## Considered alternatives

- **Make every check an LLM call** (semantic coherence, factual accuracy) — rejected: defeats the purpose of "cheap end," introduces non-determinism, makes re-runs cost money, and gates the trust state on the same model that produced the derivation (no independent signal).
- **Skip checks; trust the LLM** — rejected: this is the "stopping early on a subtly-wrong synthesis" failure mode ADR-0004 exists to prevent. A node without a provenance edge or without a synthesis marker is structurally broken, regardless of how good the prose reads.
- **One check per CLI command** (`memex check <node-id>`) — deferred: the current design runs checks inside `derive` so trust state is set atomically with creation. A standalone `check` command is a small addition when re-checking an existing node becomes a real need (e.g. after rules change).