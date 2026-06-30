# Derivations are created lazily, on a density-or-demand trigger

A higher-level derivation materializes only when **(1) density** — enough lower material has accumulated to be worth synthesizing — or **(2) demand** — queries repeatedly hit a region, so caching a high-level synthesis repays. No eager construction of full pyramids.

## Consequences

Extra abstraction levels are not free: each adds fidelity loss (a telephone-game over summaries-of-summaries), maintenance, and a longer staleness-propagation wave. Lazy creation concentrates the token-saving benefit where it actually pays. The accepted cost is **cold-start latency** on the first query into a cold region — acceptable for a single-user tool.
