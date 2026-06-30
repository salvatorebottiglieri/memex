# Two typed edge classes: provenance vs association

The graph has two non-interchangeable edge classes. **Provenance** (`derived_from`): vertical, mandatory, acyclic, the sole basis that can justify a claim. **Association** (`related` | `contradicts` | `refines`): lateral, optional, lower-trust, may be cyclic / cross-tier / cross-domain, carries a rationale and a `suggested`(agent)/`confirmed`(me) state, and **never** counts as support for a claim.

Mantra: **associations inspire, provenance justifies.** The agent may traverse association edges to pull in distant concepts, but anything it then asserts must be re-grounded via provenance or flagged `> Synthesis:`.

## Considered Options

- **One generic edge type** — rejected: either speculative "this reminds me of X" links end up counting as proof (audit breaks), or, to prevent that, creative cross-links are forbidden (serendipity dies).
- **Two typed classes** (chosen) — keeps both rigor and serendipity.

## Consequences

`contradicts` edges feed the review risk-triggers in ADR-0004. Start with the three typed relations; more emerge under the gate from ADR-0002.
