# Staleness propagation via contested state and human review

A node can become `stale` for two reasons: re-ingest of an L0 with new upstream content, or human adjudication of a `contested` derivation. This ADR covers the second path: how a `contradicts` association edge, a failed deterministic check, or another contestable occurrence becomes a `stale` trust state without bypassing human review.

## What gets added

A new **contested** flag on `node`, orthogonal to trust state. A new **contestation event** audit log. A **review proposal** written by an LLM agent. Three human commands to adjudicate: `accept`, `reject`, `dismiss`. See `CONTEXT.md` for the precise glossary.

The model distinguishes `stale` (decided, do not trust) from `contested` (in doubt, awaiting review). A node is `contested` if at least one open contestation event covers it. `is_contested` is materialised; the cover-set is a separate `event_node_link` table that supports the 1:N mapping between events and the nodes they touch.

## Propagation

When a `contradicts` edge is written, in a single transaction:

1. Insert a row in `event_queue` with `event_type = 'contradicts_edge_needs_review'`, the edge id, and the target node id.
2. Walk the provenance DAG upward from the target (recursive CTE on `edge` joining `e.to_node = ancestor.id`; see `store.find_provenance_descendants`) and insert one `event_node_link` row per ancestor found. An ancestor is **descendant of the target in the provenance graph** — i.e. a derivation that transitively depends on the target.
3. For each newly-linked node whose `is_contested` was 0, set `is_contested = 1` and `contested_at = now`.

There is **no fast-path**: every `contradicts` edge produces a contestation event, regardless of authorship or trust of the involved nodes. Stale is never reached except by explicit human adjudication. This is deliberate (ADR-0004 spirit) — the only shortcut would be human-authored edges on already-verified nodes, but "I wrote a contradiction" is not "I have decided the damage is total." Triage exists exactly to revisit that.

`trust_state` is never modified by the propagation. A `human-approved` node can be `contested`; the approval is suspended, not revoked.

## Adjudication

Three human commands close an event:

- **`memex review accept <proposal-id>`** — the damage is real. For every node in `proposal.affected_node_ids`, set `trust_state = 'stale'`. Delete the event's `event_node_link` rows. For each formerly-linked node, recompute `is_contested`: if no other open event covers it, set `is_contested = 0` and `contested_at = NULL`.
- **`memex review reject <proposal-id>`** — the contestation is unfounded (the asserting node was wrong). Delete the event's `event_node_link` rows. Recompute `is_contested` as above. `trust_state` is untouched; a `human-approved` node stays approved.
- **`memex review dismiss <proposal-id>`** — the contestation is valid but the damage is zero (no node is materially affected). Same effect as `reject` on the link table; differs only in the recorded `status`, which captures the human's reasoning for audit.

Re-derivation of `stale` nodes is **lazy** (ADR-0003): the next `memex derive` notices the `stale` trust state and regenerates. No background job.

## Review agent

The review proposal is produced by an LLM agent invoked by `memex review`. The agent is **shared with the deriver** via the existing `MEMEX_AGENT` seam; what differs is the system prompt and the input shape, not the underlying model. The agent receives the target node's content, the asserting node's content, and the payload of the `contradicts` edge, and returns a structured `ReviewProposal` (`affected_node_ids`, `damage_boundary_node_id`, `rationale_md`, `confidence`).

This is deliberately **not a deterministic check** (ADR-0011 excludes it). The review agent is a non-deterministic proposal step whose output the human is expected to read, not rubber-stamp.

## Edge authorship

A new `edge.written_by` column with values `human | llm | check | system`, default `human`. The deriver writes `llm`; the review agent writes `llm`; future deterministic checks write `check`; the staleness propagation itself writes `system`. Authorship is audit metadata and does not influence propagation policy (since there is no fast-path).

## Considered alternatives

- **Fast-path for trusted edges** (skip triage when `written_by = human` and both nodes ≥ `auto-verified`) — rejected: removes the review step for cases where the human is the only possible source of error. ADR-0004 is explicit that the human is not above review.
- **Fifth trust state `contested`** instead of an orthogonal flag — rejected: changes every `WHERE trust_state IN (...)` query and redefines the trust state machine; a boolean keeps ADR-0004's chain intact and makes rollback trivial (`is_contested = 0`).
- **`contested_event_id` column on `node`** (1:1 mapping) — rejected: the cover-set is 1:N (an event fans out to many nodes; multiple events can cover one node). A single column produces silent ownership collisions where one event's adjudication fails to clear nodes that a later event had re-marked. A separate `event_node_link` table makes the 1:N explicit.
- **Coalesce multiple events on the same target into one** — rejected: a single proposal cannot describe two genuinely-distinct contradictions on the same node from different sources. The agent's analysis is per-event, not per-target.

## Consequences

- **No silent state changes** — trust state only changes on explicit human adjudication. Propagation is reversible by construction (every contested node has at least one open event link).
- **Multiple events are first-class** — two contradictions on the same node produce two events, two proposals, two decisions. The cover-set is recomputed per adjudication, so events are isolated.
- **LLM cost is bounded by the queue** — `memex review` reads only events with no proposal yet. Idempotent re-runs do not double-invoke the agent once a proposal exists (UNIQUE on `event_id` enforces this at the DB level).
- **`is_contested` is derived state** — must be kept in sync with `event_node_link`. The two write paths (open event, close event) update both atomically. A read of `is_contested` without consulting the link table is incorrect.
- **Smoke test coverage required** — the new commands (`memex review` and its sub-commands) need end-to-end coverage. The existing duplicate schema in `tests/test_checks.py` must be updated to include the new columns and tables.