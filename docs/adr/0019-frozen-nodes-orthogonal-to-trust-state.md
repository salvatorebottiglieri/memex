# ADR-0019: Locator integrity checks and frozen nodes

Status: **Accepted**.

## Context

memex is a two-store system (ADR-0008): structure lives in SQLite, content
lives in markdown files. Locators cross that boundary in both directions —
`node.content_path` points at a file, and `derived_from` edges point at other
nodes — but nothing re-validated them against the vault as a whole. The
deterministic checks (ADR-0011) gate one derivation at a time during
`derive`; they do not answer "are all the locators in this vault still
correct?".

The Harness Handbook pattern is the reference: every active locator must be
re-validated against the current repository, and stale locators are
**frozen, not deleted**. Deleting loses the audit trail; leaving a broken
node active invites retrieval through a locator that no longer resolves.

## Decision

### Read-only vault-wide check

A new pure module, `memex.integrity`, exposes `check_integrity(locations,
provenance, sources)`. It classifies three classes of broken locator:

| Code | Meaning |
|---|---|
| `missing_content` | a non-null `content_path` does not exist |
| `content_not_a_file` | the path exists but is not a regular file |
| `content_unreadable` | the filesystem rejected `stat` (permission/OS error) |
| `dangling_provenance` | a `derived_from` edge targets a node that does not exist |
| `dead_source` | a source row is already marked `failed` |

`memex check` is read-only. It prints the findings as JSON and exits 1 when
the list is non-empty. It does not probe HTTP: a manual/on-demand probe is
deferred, and a source already marked `failed` is enough signal for now.

The check receives **data**, not a `Store`: the CLI performs the three reads
(`list_content_locations`, `list_edges`, `list_sources`) in
`_integrity_inputs` and passes immutable lists to the pure check. This keeps
the check testable with three lists and prevents it from growing SQL or
transaction capabilities it does not need.

### Frozen is a flag, not a trust state

`frozen_at` and `frozen_reason` are nullable columns on `node`, orthogonal to
`trust_state` — the same shape as `is_contested` / `contested_at` (ADR-0012).
A frozen node stays in the graph as historical reference but is excluded from
active surfaces:

- `memex list` excludes it unless `--include-frozen` is passed;
- `memex search` skips it in both the derivation file scan and the L0
  metadata query;
- the retrieval rules A1/A2 in the ontology require descent past it.

The shared SQL predicate is `Store.ACTIVE_PREDICATE` (`n.frozen_at IS NULL`).
`search` applies the same condition in its file-scan loop, because search is
not a single SQL query. `list` and `search` are viewing surfaces: they keep
showing `draft` and `contested` nodes as before, so the shared predicate
carries only the frozen exclusion. The trust/contested half of rules A1/A2
stays in the ontology, where it governs retrieval.

### Freezing is a human decision

`memex check` only reports. `memex freeze <id> --reason <text>` marks a node
(recording when and why); `memex thaw <id>` clears the flag. Freezing is never
automatic: the ticket's constraint and ADR-0012's human-adjudication model
both require that the decision stay with the human. A check that wrote would
not be re-runnable or comparable between runs.

### Frozen nodes appear in summaries

`Store.get_stats()` reports `frozen`, and `memex sync` includes that count in
its JSON summary. Frozen nodes are not removed from the vault and are still
visible in `show` and `render`.

## Considered alternatives

- **A fifth `trust_state` value** — rejected: `trust_state` is an ordinal
  fidelity ladder (`draft > auto-verified > human-approved > stale`) used as a
  filter throughout the CLI and ontology. `frozen` is not less trusted; it is
  inactive. Putting it in the ordinal breaks existing queries and the model.
- **Two separate conditions, one per command** — rejected: `list` filters in
  SQL and `search` scans files, so two local conditions would drift apart.
  One named predicate is the single point of change.
- **Extending `checks.py` with D7/D8** — rejected: `checks.py` is the
  per-derivation draft→auto-verified gate, bounded to one node during
  `derive`. The locator check is vault-wide, reads the filesystem, and runs on
  demand; sharing the module would make every derivation pay for a check that
  does not concern it.
- **Auto-freezing findings inside `check`** — rejected: destroys the
  read-only property, makes the check non-idempotent, and removes human
  adjudication.
- **Deleting broken nodes** — rejected: the graph keeps the historical
  reference; removal, if ever needed, stays an explicit human action.

## Consequences

- A DB opened by an older version is migrated in place: two nullable columns
  are added, with no table rebuild.
- `list` and `search` gain one shared exclusion; contestation and trust-state
  visibility are unchanged.
- The ontology now names `frozen_at` in the node model and in rules A1/A2.
- The check is cheap enough to run on demand and deterministic: same vault,
  same findings, no network and no LLM.
- Orphaned content files (file present, no node pointing at it) are outside
  this decision; the check follows locators from the graph to the filesystem.
