# Trust-state machine gates the agent's right to stop; human review is targeted

Every node has a trust state: `draft → auto-verified → human-approved → stale`. A node also has a `contested` flag, orthogonal to trust state (see ADR-0012). **The agent may stop on a node during top-down navigation only if `trust_state ∈ {auto-verified, human-approved}` AND `is_contested = 0`**; otherwise it must descend. A stop on a non-`human-approved` node for a consultation I care about yields a low-confidence answer and queues the node for review. Human review is mandatory only for the high tiers I consult and for **risk triggers** (failed deterministic check, detected `contradicts` edge, low confidence, or tier over-claim), governed by a configurable policy (which tiers, which triggers, batch/on-consultation/sampled, per-session budget).

## Consequences

This is the direct mitigation for "stopping early on a subtly-wrong synthesis." My review time is the scarcest resource in the system; reviewing everything would kill it by attrition, so review is spent surgically. Deterministic checks (the cheap end): resolvable provenance for every claim (no floating claims), no dangling refs, tier/depth consistency, not-stale-vs-parents, `> Synthesis:` marker on non-sourced claims, size/scope bounds.
