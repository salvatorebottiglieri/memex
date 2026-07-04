# Staleness propagation via contested state and human review

## Problem Statement

Today, `stale` is a trust state that exists in the schema but is never written. When a derivation turns out to be wrong — the LLM produced an inaccurate summary, an L0 was misunderstood, or two derivations end up contradicting each other — there is no path from "auto-verified" (or worse, "human-approved") to "stale" that does not involve direct manual intervention at the SQL level. The user has no queue to look at, no agent to consult, and no audit trail of why a node was or was not invalidated.

In practice this means: (a) stale information sits in the graph and the agent may stop on it, (b) a `contradicts` association edge is a statement without consequences, and (c) the only escalation path is "find the L0, re-ingest, hope it propagates" — but L0 is immutable by design. The system is missing a real adversarial review process.

## Solution

Add a **contested** flag on `node`, orthogonal to trust state. A new **contestation event** audit log captures what put a node into `contested`. A **review agent** (an LLM with a separate system prompt) analyses each event and produces a **review proposal** identifying which nodes are materially affected. Three human commands — `accept`, `reject`, `dismiss` — adjudicate the proposal. The agent never reaches `stale` on its own; only the human can move a node there, and only after reading the agent's analysis.

The user is the bottleneck. The system surfaces events in a review queue, the user picks one, reads the proposal, and decides. Two events can target the same node. A node remains `contested` only as long as at least one open event covers it.

The visible change from the user's perspective:

- A `memex review` command reads pending events and produces proposals.
- A `memex review list` command shows the queue.
- Three `memex review <verb> <proposal-id>` commands to close events.
- A `memex show` on a node now reports `is_contested` and (if contested) the contestation event id.
- A `memex list` output includes the `is_contested` flag.
- A `memex render` markdown frontmatter includes `trust_state/contested` as a tag.

The system gains: a queue the user can act on, an audit trail of every contestation, a review agent whose output the human reads, and a clean separation between "in doubt" (contested) and "decided wrong" (stale).

## User Stories

1. As a user, I want a `contradicts` association edge to put the target node into `contested`, so that the agent can no longer treat it as a reliable stopping point.
2. As a user, I want a `contradicts` edge to also mark every derivation that depends on the target, so that the entire pyramid above the contested node is flagged.
3. As a user, I want a `contested` node to retain its existing trust state (auto-verified, human-approved), so that an in-progress review does not silently demote my prior approval.
4. As a user, I want a review queue that lists every contestation event awaiting analysis, so that I can pick one when I have time.
5. As a user, I want `memex review` to invoke an LLM agent that produces a review proposal for each pending event, so that I do not have to figure out the damage boundary myself.
6. As a user, I want the review proposal to identify the specific nodes that are materially affected, so that I can decide on a precise scope.
7. As a user, I want the review proposal to include a free-form rationale written by the agent, so that I can understand its reasoning before deciding.
8. As a user, I want the review proposal to include a confidence level (high | medium | low), so that I can calibrate how much to trust it.
9. As a user, I want to `accept` a review proposal, so that the affected nodes go to `stale` and the affected-event link is removed.
10. As a user, I want to `reject` a review proposal, so that the affected-event link is removed but the trust state of every node is preserved.
11. As a user, I want to `dismiss` a review proposal, so that the system records that the contestation was valid but caused no damage.
12. As a user, I want the difference between `reject` and `dismiss` to be visible in the audit log, so that I can later tell which events I treated as unfounded vs. valid-but-harmless.
13. As a user, I want two different `contradicts` edges on the same target node to produce two independent events, so that distinct contradictions get distinct review proposals.
14. As a user, I want a `contested` node that is the target of multiple open events to remain `contested` after the first event is closed, so that other pending events still cover it.
15. As a user, I want a `contested` node to become un-contested only when the last open event covering it is closed, so that the `is_contested` flag is always backed by at least one open event.
16. As a user, I want `memex show` on a node to surface `is_contested` and the `contested_at` timestamp, so that I can see contestation state at a glance.
17. As a user, I want `memex show` on a node to surface the id of the open contestation event(s) covering it, so that I can route to a review.
18. As a user, I want `memex list` to include the `is_contested` flag in its output, so that I can scan for contested nodes in bulk.
19. As a user, I want the markdown frontmatter rendered by `memex render` to include a `trust_state/contested` tag when applicable, so that Obsidian surfaces the state.
20. As a user, I want the agent's "can I stop here" rule to be updated so that a node is stoppable only if `trust_state ∈ {auto-verified, human-approved}` AND `is_contested = 0`, so that I never stop on contested information.
21. As a user, I want the review agent to use the same LLM seam (`MEMEX_LLM_MODULE`) as the deriver, so that I do not need a second env var or a second test-injection module.
22. As a user, I want the review agent to have a different system prompt from the deriver, so that its role (analysis, not generation) is clear to the model.
23. As a user, I want the review agent to receive both the target node's content and the asserting node's content, so that it can reason about the contradiction.
24. As a user, I want the review agent to receive the `contradicts` edge payload, so that it understands the reason given for the contradiction.
25. As a user, I want the review agent's return shape to be a structured `ReviewProposal` (affected_node_ids, damage_boundary_node_id, rationale_md, confidence), so that the result is machine-parseable and stored uniformly.
26. As a user, I want the review agent to be tested via the same `FakeLLMClient` injection used by the deriver, so that the test surface stays uniform.
27. As a user, I want `memex review` to be safe to re-run, so that if it is interrupted mid-batch I can run it again without double-processing.
28. As a user, I want `memex review accept` to be idempotent, so that running it twice on the same proposal does not corrupt state.
29. As a user, I want `memex review list` to show both pending events without proposals and pending proposals awaiting my decision, so that I have one view of the queue.
30. As a user, I want edge authorship to be tracked on every edge, so that I can audit which edges came from LLM, human, or future deterministic checks.
31. As a user, I want the deriver to tag its `derived_from` edges as `written_by = 'llm'`, so that audit metadata is correct.
32. As a user, I want new edges written directly by me (via CLI) to default to `written_by = 'human'`, so that I do not have to remember to tag them.
33. As a user, I want the contested footprint of a single event to be stored explicitly (one row per node covered), so that closing the event can roll back the footprint atomically.
34. As a user, I want a node that has been adjudicated `stale` to be re-derivable lazily on the next `memex derive`, so that no background job is required.
35. As a user, I want the deterministic checks (ADR-0011) to be unaffected by the review agent, so that the cheap-end gate stays cheap and deterministic.
36. As a user, I want the existing smoke tests to gain coverage of the new commands, so that regressions in the review flow are caught end-to-end.

## Implementation Decisions

### Schema

**`node`** gains two columns:

- `is_contested INTEGER NOT NULL DEFAULT 0` — materialised flag, always consistent with the `event_node_link` table on write paths.
- `contested_at TEXT` — timestamp of the most recent transition to `is_contested = 1`. NULL when not contested.

**`edge`** gains one column:

- `written_by TEXT NOT NULL DEFAULT 'human' CHECK (written_by IN ('human','llm','check','system'))` — audit metadata. The deriver writes `'llm'`; the review agent writes `'llm'` when it inserts an edge; the staleness propagation itself writes `'system'`. Default is `'human'` for any edge written by the CLI directly.

**`event_queue`** (new table):

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `event_type TEXT NOT NULL CHECK (event_type IN ('contradicts_edge_needs_review', ...))` — the trailing `, ...` reserves room for future event types (failed deterministic checks, etc.) without a schema migration. The CHECK constraint lists only types that are actually implemented.
- `edge_id TEXT NOT NULL REFERENCES edge(id)` — the edge that triggered the event.
- `target_node_id TEXT NOT NULL REFERENCES node(id)` — the node that was directly contested.
- `created_at TEXT NOT NULL`
- `status TEXT NOT NULL CHECK (status IN ('pending','closed')) DEFAULT 'pending'`
- `closed_at TEXT` — nullable; set on adjudication.
- Index on `(status)` for the "pending" query. Index on `(target_node_id)` for "show events for node X".

**`event_node_link`** (new table, the key 1:N representation):

- `event_id INTEGER NOT NULL REFERENCES event_queue(id)`
- `node_id TEXT NOT NULL REFERENCES node(id)`
- `contested_at TEXT NOT NULL`
- `PRIMARY KEY (event_id, node_id)` — guarantees idempotent inserts and efficient deletes-by-event.
- Index on `(node_id)` for "find all open events covering this node" (the `is_contested` recomputation).
- Index on `(event_id)` is implicit in the primary key.

**`review_proposal`** (new table):

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `event_id INTEGER NOT NULL UNIQUE REFERENCES event_queue(id)` — 1:1 with event; the UNIQUE constraint enforces the 1:1 invariant at the DB level, so a re-run of `memex review` cannot write a second proposal for the same event.
- `affected_node_ids TEXT NOT NULL` — JSON list of `node.id` strings. The agent's declared set of materially affected nodes.
- `damage_boundary_node_id TEXT REFERENCES node(id)` — nullable; the agent's "deepest affected node" claim, derived from `affected_node_ids` (the node with the highest `depth` in the list). Operationally inert: the accept rule uses `affected_node_ids`, not the boundary. The boundary is for the human's reading.
- `rationale_md TEXT NOT NULL` — free-form markdown from the agent.
- `confidence TEXT NOT NULL CHECK (confidence IN ('high','medium','low'))`
- `status TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected','dismissed')) DEFAULT 'pending'`
- `human_note TEXT` — nullable; the human's note on adjudication (optional).
- `created_at TEXT NOT NULL`
- `resolved_at TEXT` — nullable; set on adjudication.
- Index on `(status)` for the review queue query.

### Migration

The new columns and tables are added inside the existing `init_schema` flow. The pattern of `try/except sqlite3.OperationalError` around `ALTER TABLE ADD COLUMN` continues for the new `node` and `edge` columns. The new tables are `CREATE TABLE IF NOT EXISTS` in the schema SQL. No version tracking, consistent with the existing project pattern.

### Module changes

**`src/memex/store.py`**:

- New methods: `open_contestation_event(edge_id, target_node_id) -> event_id`, `link_event_to_node(event_id, node_id, contested_at)`, `find_provenance_descendants(target_node_id) -> list[node_id]`, `get_node_open_events(node_id) -> list[event_id]`, `get_pending_events_without_proposal() -> list[event]`, `write_review_proposal(...)`, `get_review_queue() -> list[proposal]`, `accept_proposal(proposal_id, human_note=None)`, `reject_proposal(proposal_id, human_note=None)`, `dismiss_proposal(proposal_id, human_note=None)`.
- The `create_edge` signature gains a `written_by: str = "human"` keyword argument; the existing call sites continue to work because of the default.
- The `get_node` and `list_nodes` SELECT lists gain `is_contested` and `contested_at` so `memex show` and `memex list` can surface them.
- The `is_contested` flag is kept in sync with `event_node_link` inside the same transactions that insert/delete link rows. The store's `open()` context manager handles the commit; no explicit transactions are added.

**`src/memex/llm_client.py`**:

- The `LLMClient` base class gains a `review(target_content, asserting_content, edge_payload) -> ReviewProposal` method, alongside the existing `derive`. Same seam (`MEMEX_LLM_MODULE`), same test-injection surface.
- A `ReviewProposal` dataclass is added (mirroring the existing `DerivationResult`).
- A `load_llm_client` change is **not** needed: the seam loads the same class, which now exposes two methods.
- The Anthropic implementation adds a second method with a different system prompt; the JSON parsing logic mirrors the deriver's `prose` / `synthesis_statements` pattern, now for `affected_node_ids` / `damage_boundary_node_id` / `rationale_md` / `confidence`.

**`src/memex/cli.py`**:

- A new `review` group with five sub-commands: `memex review`, `memex review list`, `memex review accept <proposal-id>`, `memex review reject <proposal-id>`, `memex review dismiss <proposal-id>`.
- The `show` and `list` commands need no signature change, but the JSON output gains `is_contested` and `contested_at` because the store methods now return them.
- The `derive` command's `create_edge` call passes `written_by='llm'`.

**`src/memex/renderer.py`**:

- `_build_frontmatter` gains a `trust_state/contested` tag when `is_contested = 1`. No other rendering changes.

### Propagation

When a `contradicts` edge is inserted via `create_edge` (or a future store-level wrapper for the same), the `open_contestation_event` is called in the same transaction. The flow:

1. `create_edge` (now with `written_by`).
2. `find_provenance_descendants(target_node_id)` — recursive CTE on `edge` joining `e.to_node = ancestor.id` (the project's convention: `from_node` is the derivation, `to_node` is the source). Returns all nodes that transitively depend on the target.
3. `open_contestation_event` — inserts the event row.
4. For each descendant, `link_event_to_node` inserts a `event_node_link` row and, if `is_contested` was 0, sets `is_contested = 1` and `contested_at = now`.
5. Single `Store.open` transaction commits everything.

There is no fast-path. Every `contradicts` edge produces a contestation event regardless of authorship or trust.

### Adjudication

The three human commands operate on a single `review_proposal`. Each runs inside a `Store.open` transaction:

1. **`accept`** — for every node in `proposal.affected_node_ids`, `UPDATE node SET trust_state = 'stale'`. Then `DELETE FROM event_node_link WHERE event_id = ?`. For each formerly-linked node, recompute `is_contested`: if no other open event covers it (no row in `event_node_link` for this node, joined with `event_queue.status = 'pending'`), set `is_contested = 0` and `contested_at = NULL`. Set `event_queue.status = 'closed'`, `closed_at = now`. Set `proposal.status = 'accepted'`, `resolved_at = now`.
2. **`reject`** — same effect on `event_node_link` and `is_contested`. `trust_state` is untouched. `event_queue.status = 'closed'`. `proposal.status = 'rejected'`.
3. **`dismiss`** — same effect on `event_node_link` and `is_contested`. `trust_state` untouched. `event_queue.status = 'closed'`. `proposal.status = 'dismissed'`.

Re-derivation of `stale` nodes is lazy (per ADR-0003): the next `memex derive` notices the `stale` trust state on the L0 and regenerates the derivation chain. No background job.

### Review agent shape

The agent receives:

- `target_content` (str) — the content of the target node (the one being contested).
- `asserting_content` (str) — the content of the asserting node (the one that wrote the `contradicts` edge).
- `edge_payload` (dict) — metadata from the `contradicts` edge: the relation, the creation time, any payload fields. (For now this is the standard edge metadata; the schema does not store free-form payload, so this is a placeholder for future richness.)

The agent returns a `ReviewProposal` with `affected_node_ids` (list of `node.id`), `damage_boundary_node_id` (optional, the deepest in the list by `depth`), `rationale_md` (markdown text), and `confidence` (`high` | `medium` | `low`).

The system prompt (stored in `llm_client.py` next to the deriver's) instructs the model to identify the subset of descendants that materially depend on the contested claim, distinct from descendants that happen to transitively include the target but do not rely on the contested content.

### Idempotency

- **`memex review` re-run** — reads events with `status='pending'` and no `review_proposal` (LEFT JOIN with `triage_proposal` returning NULL). If interrupted after LLM call but before INSERT, the next run re-invokes the LLM (cost paid twice, but the result is still correct). If interrupted after INSERT but before commit, the transaction rolls back. The UNIQUE on `review_proposal.event_id` makes a double-INSERT a hard error, which is the right behaviour: the agent has been called once, the result is durable.
- **`memex review accept` re-run** — the second call sees `event_queue.status = 'closed'`, `proposal.status = 'accepted'`, and short-circuits with a "already_accepted" response. No state is touched.

## Testing Decisions

**What makes a good test for this feature:** the test asserts on the observable behaviour of the CLI (JSON output, DB state) and on the `is_contested` / `trust_state` of nodes after adjudication, not on the internal `event_node_link` row count. The review agent is a non-deterministic step; tests inject a `FakeLLMClient` with a `review` method that returns a deterministic `ReviewProposal`.

**Modules tested:**

- `src/memex/store.py` — `open_contestation_event`, `link_event_to_node`, `find_provenance_descendants`, `accept_proposal`, `reject_proposal`, `dismiss_proposal`, `is_contested` recomputation, the `create_edge(written_by=...)` extension. The `is_contested` recomputation is the most subtle test: a node covered by two open events must remain contested after the first event is closed.
- `src/memex/llm_client.py` — `LLMClient.review` default raise; `AnthropicLLMClient.review` JSON parsing (mirrors the existing `derive` test pattern). The `FakeLLMClient` is updated to expose a `review` method returning a hand-crafted `ReviewProposal`.
- `src/memex/cli.py` — `memex review`, `memex review list`, `memex review accept/reject/dismiss`, the new `is_contested` field in `memex show` and `memex list`.
- `src/memex/renderer.py` — `trust_state/contested` tag in frontmatter.

**Prior art:**

- `tests/test_checks.py` has a hand-rolled schema fixture (lines 33-62). The fixture must be updated to include the new `node` columns, the `edge.written_by` column, and the new tables. Without this update, `run_checks` will continue to work, but `get_node` with `is_contested` in the SELECT will fail in those tests.
- `tests/smoke_test.py` has 93 end-to-end checks. A new `smoke_review` group is added that walks the full path: ingest L0 → derive → write a `contradicts` edge directly (via a small test-only CLI hook or by calling `store.create_edge` then `store.open_contestation_event`) → run `memex review` → assert a proposal is written → run `memex review accept` → assert `is_contested = 0` and `trust_state = 'stale'` on the affected node.
- `tests/fake_llm_client.py` is extended with a `review` method that returns a pre-canned `ReviewProposal` whose `affected_node_ids` is the full descendant set, `rationale_md` is a fixed string, and `confidence` is `"high"`. The existing fake's `derive` method is unchanged.
- The existing `tests/test_derive.py` will need a small update: the `create_edge` call inside the CLI's `_do_derive` now passes `written_by='llm'`; a test that asserts the edge's `written_by` value confirms the wire-through works.

## Out of Scope

- Re-ingest / refresh L0 from URL (the upstream-drift variant rejected in the design discussion).
- Time-based TTL on trust state (the time-based variant rejected in the design discussion).
- A second, separate LLM module for the review agent (the same `MEMEX_LLM_MODULE` is used; the prompt differs, not the model).
- Concurrency model beyond SQLite's default file-level locking (no WAL mode, no connection pooling).
- Schema versioning or migration history (the existing `try/except ALTER TABLE` pattern continues).
- A UI for the review queue beyond the CLI. (Obsidian frontmatter tags are the only human-facing visualisation.)
- Stale propagation re-derivation: when a node goes `stale`, the next `memex derive` regenerates it; we do not background-job it.
- The "failed deterministic check produces a contestation event" path: the schema reserves `event_type` for it, but only `contradicts_edge_needs_review` is implemented in this PRD.

## Further Notes

- The contested flag is **derived state**: it must be kept in sync with `event_node_link` by the two write paths (open event, close event). Reading `is_contested` without joining `event_node_link` is correct only if the two writes have been careful. This invariant is enforced inside the store methods, not at the DB level — the DB cannot express it.
- The review agent is **non-deterministic** and **costs money to run**. `memex review` should be invoked deliberately (cron, manual). The PRD does not decide the deployment pattern.
- The `is_contested` recomputation on close is the only multi-row UPDATE outside the propagation path. It is bounded by the number of nodes in the event's footprint, which is bounded by the depth of the provenance DAG above the target. In practice, small.
- The `ReviewProposal.affected_node_ids` JSON list bypasses FK enforcement: a node id may be deleted between proposal creation and accept. The accept handler issues `UPDATE node SET trust_state = 'stale' WHERE id IN (...)`, which silently no-ops for missing ids. The proposal's `affected_node_ids` is then stale, but the event is already closed. The PRD does not address this; it is rare in single-user mode.
- The `written_by` column has no read helper. A future `memex edges` command or a `list_edges_by_author` method would surface authorship. The PRD does not build that.
- The agent's confidence level is a free-form scalar from the LLM. There is no calibration mechanism (no ground-truth set of past proposals with known-correct adjudications). This is acceptable for a personal tool but would not survive a multi-user deployment.
