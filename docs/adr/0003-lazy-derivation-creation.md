# Lazy derivation creation — demand only

Derivations are created lazily: no eager construction of full pyramids. Two triggers were originally envisioned; only one was adopted.

- **Demand (adopted, ADR-0014):** during a consultation, the agent detects related material and proposes synthesis to the user. The user authorizes. This is the only synthesis trigger.
- **Density (deferred):** automatically detecting "enough lower material" was never specified. Deferred indefinitely — YAGNI until a concrete use case demands it.

The core principle — lazy over eager — is implemented across the board:
- `memex derive <l0-id>` — explicit, one-shot
- `memex synthesize <id1> <id2> ...` — explicit, user-authorized
- Re-derivation of stale nodes (ADR-0012) — on next explicit action

## Consequences

Extra abstraction levels are not free: each adds fidelity loss (a telephone-game over summaries-of-summaries), maintenance, and a longer staleness-propagation wave. Lazy creation concentrates the token-saving benefit where it actually pays. The accepted cost is **cold-start latency** on the first query into a cold region — acceptable for a single-user tool.
