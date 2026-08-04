"""Declarative rule engine for memex inference rules.

Single source of truth for confidence, check, and edge rules.
Rules are first-class functions — no string parsing or interpretation.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ── Size bounds ────────────────────────────────────────────────────

MIN_CHARS = 100
MAX_CHARS = 50_000


# ── Rule dataclass ─────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    """A single inference rule.

    ``condition`` signature depends on ``category``:

    - ``"confidence"``: ``condition(store, node_id: str) -> bool``
      Returns ``True`` if the rule fires on this node.

    - ``"check"``: ``condition(con: sqlite3.Connection, node_id: str,
      content_path: Path, content: str) -> list[str]``
      Returns a list of failure messages (empty = pass).
    """

    id: str
    category: str
    description: str
    condition: Callable[..., Any]
    consequence: str  # confidence label or error message prefix


# ── Confidence inference rules (priority order — first match wins) ─

def _c4_contradict_overrides(store: Any, node_id: str) -> bool:
    """C4: ANY incoming 'contradicts' edge → low."""
    count = store._con.execute(
        "SELECT COUNT(*) FROM edge WHERE to_node = ? AND type = 'association' AND relation = 'contradicts'",
        (node_id,),
    ).fetchone()[0]
    return count > 0


def _c3_multi_parent(store: Any, node_id: str) -> bool:
    """C3: 2+ incoming 'derived_from' edges → high."""
    count = store._con.execute(
        "SELECT COUNT(*) FROM edge WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'",
        (node_id,),
    ).fetchone()[0]
    return count >= 2


def _c2_single_parent(store: Any, node_id: str) -> bool:
    """C2: Exactly 1 incoming 'derived_from' edge → medium."""
    count = store._con.execute(
        "SELECT COUNT(*) FROM edge WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'",
        (node_id,),
    ).fetchone()[0]
    return count == 1


def _c1_no_parents(store: Any, node_id: str) -> bool:
    """C1: 0 incoming 'derived_from' edges → low."""
    count = store._con.execute(
        "SELECT COUNT(*) FROM edge WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'",
        (node_id,),
    ).fetchone()[0]
    return count == 0


CONFIDENCE_RULES: list[Rule] = [
    Rule(
        id="C4",
        category="confidence",
        description="Incoming contradicts overrides everything → low",
        condition=_c4_contradict_overrides,
        consequence="low",
    ),
    Rule(
        id="C3",
        category="confidence",
        description="2+ provenance parents → high",
        condition=_c3_multi_parent,
        consequence="high",
    ),
    Rule(
        id="C2",
        category="confidence",
        description="1 provenance parent → medium",
        condition=_c2_single_parent,
        consequence="medium",
    ),
    Rule(
        id="C1",
        category="confidence",
        description="0 parents → low",
        condition=_c1_no_parents,
        consequence="low",
    ),
]


# ── Extracted-node confidence (ticket #95) ──────────────────────────────

# Extracted nodes carry no provenance of their own; their confidence is set
# from the fetcher that produced them. URL nodes (the root of every chain)
# have no confidence at all — handled in ``store.compute_node_confidence``.
EXTRACTED_CONFIDENCE: dict[str, str] = {
    "http": "medium",
    "youtube": "low",
    "pdf": "high",
    # The REST summary is curated prose (parallel to pdf): it goes through
    # MediaWiki's summarization pipeline, not raw HTML scraping.
    "wikipedia": "high",
}


# ── Deterministic check rules (accumulate all failures) ────────────

def _d1_provenance_check(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D1: Node MUST have at least one incoming 'derived_from' edge."""
    edge = con.execute(
        """
        SELECT to_node FROM edge
        WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
        LIMIT 1
        """,
        (node_id,),
    ).fetchone()

    failures: list[str] = []
    if edge is None:
        failures.append(
            f"Provenance check failed: no derived_from edge found for node {node_id}"
        )
        return failures

    target = edge[0]
    target_exists = con.execute(
        "SELECT id FROM node WHERE id = ?", (target,)
    ).fetchone()
    if target_exists is None:
        failures.append(
            f"Provenance check failed: derived_from target {target!r} "
            "does not exist in the node table"
        )
    return failures


def _d2_dangling_ref_check(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D2: Every provenance target MUST exist as a node."""
    edges = con.execute(
        """
        SELECT to_node FROM edge
        WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
        """,
        (node_id,),
    ).fetchall()

    failures: list[str] = []
    for (target,) in edges:
        exists = con.execute(
            "SELECT id FROM node WHERE id = ?", (target,)
        ).fetchone()
        if exists is None:
            failures.append(
                f"Dangling reference check failed: provenance target {target!r} "
                "does not exist in the node table"
            )
    return failures


def _d3_synthesis_check(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D3: At least one synthesis statement (from DB column or file marker)."""
    ss_row = con.execute(
        "SELECT synthesis_statements FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    db_statements: list[str] = []
    if ss_row is not None and ss_row[0]:
        try:
            parsed = json.loads(ss_row[0])
            if isinstance(parsed, list):
                db_statements = [str(s) for s in parsed]
        except (json.JSONDecodeError, TypeError):
            pass

    file_statements = re.findall(r"> Synthesis:\s*(.*)", content)

    if not db_statements and not file_statements:
        return [
            'Synthesis marker check failed: derivation must contain at least one '
            '"> Synthesis:" statement (or have a non-empty synthesis_statements column)'
        ]

    if db_statements and file_statements:
        if len(db_statements) != len(file_statements):
            return [
                f'Synthesis marker check failed: {len(db_statements)} DB synthesis '
                f'statements but {len(file_statements)} file markers'
            ]
        for i, (db_s, file_s) in enumerate(zip(db_statements, file_statements)):
            if db_s.strip() != file_s.strip():
                return [
                    f'Synthesis marker check failed: content mismatch at index {i}: '
                    f'DB={db_s!r} vs file={file_s!r}'
                ]

    return []


def _d4_size_bounds(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D4: Content length between MIN_CHARS and MAX_CHARS."""
    length = len(content)
    if length < MIN_CHARS:
        return [
            f"Size check failed: derivation is too short ({length} chars, minimum is {MIN_CHARS})"
        ]
    elif length > MAX_CHARS:
        return [
            f"Size check failed: derivation is too long ({length} chars, maximum is {MAX_CHARS})"
        ]
    return []


def _d5_tier_depth(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D5: Tier/depth consistency: L0=0, notes=1, synthesis>=2."""
    row = con.execute(
        "SELECT tier, depth FROM node WHERE id = ?",
        (node_id,),
    ).fetchone()

    if row is None:
        return []

    tier, depth = row[0], row[1]
    if tier is None or tier == "":
        if depth != 0:
            return [
                f"Tier/depth inconsistency: node has no tier but depth={depth} (expected 0)"
            ]
    elif tier == "notes":
        if depth != 1:
            return [
                f"Tier/depth inconsistency: tier=notes but depth={depth} (expected 1)"
            ]
    elif tier == "synthesis":
        if depth < 2:
            return [
                f"Tier/depth inconsistency: tier=synthesis but depth={depth} (expected >= 2)"
            ]

    return []


CHECK_RULES: list[Rule] = [
    Rule(
        id="D1",
        category="check",
        description="Node must have at least one incoming derived_from edge",
        condition=_d1_provenance_check,
        consequence="Provenance check",
    ),
    Rule(
        id="D2",
        category="check",
        description="Every provenance target must exist as a node",
        condition=_d2_dangling_ref_check,
        consequence="Dangling reference check",
    ),
    Rule(
        id="D3",
        category="check",
        description="Synthesis statements match file markers (count + per-index content)",
        condition=_d3_synthesis_check,
        consequence="Synthesis marker check",
    ),
    Rule(
        id="D4",
        category="check",
        description="Content size between MIN_CHARS and MAX_CHARS",
        condition=_d4_size_bounds,
        consequence="Size check",
    ),
    Rule(
        id="D5",
        category="check",
        description="Tier/depth consistency: L0=0, notes=1, synthesis>=2",
        condition=_d5_tier_depth,
        consequence="Tier/depth check",
    ),
]

# ── Ontology generation ────────────────────────────────────────────


def render_ontology() -> str:
    """Generate the full docs/ONTOLOGY.md content from the Rule registry."""
    confidence_ids = [r.id for r in CONFIDENCE_RULES]  # list order = priority order
    check_ids = [r.id for r in CHECK_RULES]

    # Dynamic section 3 — Confidence inference rules
    sec3_intro = (
        f"See `src/memex/rules.py` for the declarative rule definitions "
        f"({confidence_ids[-1]}\u2013{confidence_ids[0]}).\n"
        f"The rules are evaluated in priority order "
        f'({" > ".join(confidence_ids)}); first match wins.\n'
    )

    # Dynamic section 7 — Deterministic checks
    sec7_intro = (
        f"See `src/memex/rules.py` for the declarative check definitions "
        f"({check_ids[0]}\u2013{check_ids[-1]}).\n"
        f"Constants `MIN_CHARS` and `MAX_CHARS` live in `rules.py`.\n"
    )

    return (
        "# memex — Ontology & Inference Rules\n"
        "\n"
        "This document captures memex's knowledge model as an explicit ontology: entity types,\n"
        "relationship types, constraints, and inference rules. The rules are **not** aspirational —\n"
        "they are extracted from the code (`store.py`, `checks.py`, `agent.py`, `cli.py`, ADRs).\n"
        "Every rule here has a concrete implementation.\n"
        "\n"
        "## 1. Entity model\n"
        "\n"
        "### 1.1 Node\n"
        "\n"
        "| Property | Type | Cardinality | Description |\n"
        "|---|---|---|---|\n"
        "| `id` | UUID (str) | 1 | Unique identifier |\n"
        "| `kind` | enum | 1 | `raw_source` | `summary` | (open vocabulary) |\n"
        "| `tier` | enum | null | 0–1 | `raw` | `notes` | `synthesis` (fixed spine, ADR-0002). Null = L0 |\n"
        "| `trust_state` | enum | 1 | `draft` > `auto-verified` > `human-approved` > `stale` (strict ordinal) |\n"
        "| `depth` | int | 1 | Computed: `max(parent.depth) + 1`. L0 = 0 |\n"
        "| `confidence` | enum | 1 | `high` > `medium` > `low`. Inherited from parents |\n"
        "| `is_contested` | bool | 1 | Orthogonal to trust_state. `1` if any open `event_node_link` exists |\n"
        "| `contested_at` | ISO-8601 | null | 0–1 | When first contested |\n"
        "| `content_path` | path | 1 | Filesystem path to markdown file |\n"
        "| `synthesis_statements` | JSON array | null | 0–1 | Structured list of inferences |\n"
        "| `check_failures` | JSON array | null | 0–1 | Deterministic check failures (if any) |\n"
        "| `created_at` | ISO-8601 | 1 | Creation timestamp |\n"
        "\n"
        "### 1.2 Edge\n"
        "\n"
        "| Property | Type | Cardinality | Description |\n"
        "|---|---|---|---|\n"
        "| `id` | UUID (str) | 1 | Unique identifier |\n"
        "| `type` | enum | 1 | `provenance` | `association` |\n"
        "| `relation` | enum | 1 | `derived_from` | `related` | `contradicts` | `refines` |\n"
        "| `from_node` | UUID | 1 | Source node |\n"
        "| `to_node` | UUID | 1 | Target node |\n"
        "| `written_by` | enum | 1 | `human` | `llm` | `check` | `system` |\n"
        "\n"
        "### 1.3 Event queue\n"
        "\n"
        "See [Contestation & review](#6-contestation--review).\n"
        "\n"
        "### 1.4 Source (ledger)\n"
        "\n"
        "| Property | Type | Description |\n"
        "|---|---|---|\n"
        "| `node_id` | UUID | FK → node.id |\n"
        "| `canonical_key` | str (UNIQUE) | Dedup identity (normalized URL or platform id) |\n"
        "| `source_url` | str | Original URL |\n"
        "| `title` | str | null | Extracted title |\n"
        "| `fetched_at` | ISO-8601 | null | Last fetch timestamp |\n"
        "| `failed` | int (bool) | `1` if last fetch failed |\n"
        "\n"
        "---\n"
        "\n"
        "## 2. Edge invariants\n"
        "\n"
        "```\n"
        "Rule E1 — Edge type partitions\n"
        "  type = 'provenance'  ∧  relation = 'derived_from'  ⇒  provenance edge\n"
        "  type = 'association' ∧  relation ∈ {'related','contradicts','refines'}  ⇒  association edge\n"
        "\n"
        "Rule E2 — Provenance edges are acyclic\n"
        "  There exists no directed cycle of provenance edges.\n"
        "  (Enforced by computed depth monotonicity: depth strictly increases.)\n"
        "\n"
        "Rule E3 — Provenance edges are vertical\n"
        "  A provenance edge connects a derivation to the node(s) it was derived from.\n"
        "  L0 nodes have no incoming provenance edges.\n"
        "\n"
        "Rule E4 — Associations carry no evidentiary weight\n"
        "  Association edges (related, contradicts, refines) never count as support for a claim.\n"
        "  Only provenance edges can justify a claim.\n"
        "```\n"
        "\n"
        "Implementations:\n"
        "- **E1**: DB CHECK constraint on `edge.type` and `edge.relation`\n"
        "- **E2**: Not enforced at DB level; depth invariant in code preserves it\n"
        "- **E4**: Enforced in arch design (ADR-0005); `confidence` formula ignores association edges as parent count\n"
        "\n"
        "---\n"
        "\n"
        "## 3. Confidence inference rules\n"
        "\n"
        + sec3_intro
        + "\n"
        + "```\n"
        + "Rule C5 \u2014 Synthesis confidence is the minimum of parents\n"
        + "  FOR each synthesis node S:\n"
        + "    S.confidence = min(P.confidence for P in provenance_parents(S))\n"
        + "    where min('high', 'medium', 'low') follows: high > medium > low\n"
        + "  (Applied on creation and recomputed on contradiction cascades.)\n"
        + "\n"
        + "Rule C6 \u2014 Confidence recomputation is lazy\n"
        + "  Confidence is recomputed eagerly only when:\n"
        + "    - A 'contradicts' edge is written (cascade)\n"
        + "    - _backfill_confidence() runs (one-time migration)\n"
        + "  Other edge writes do NOT trigger recomputation.\n"
        + "```\n"
        + "\n"
        + "Implementation: `store.compute_node_confidence()` delegates to `CONFIDENCE_RULES` in `rules.py`. C5 applied in `store._backfill_confidence()` and `store._propagate_contradiction()`.\n"
        + "\n"
        + "---\n"
        "\n"
        "## 4. Trust state inference rules\n"
        "\n"
        "```\n"
        "Rule T1 — Trust state ordinal\n"
        "  trust_state follows strict ordinal:\n"
        "    human-approved  >  auto-verified  >  draft  >  stale\n"
        "\n"
        "Rule T2 — Provenance trust cascade (one-way down)\n"
        "  IF node P.trust_state regresses (moves to a lower ordinal)\n"
        "     AND node C is a direct derivation of P (C → P via 'derived_from')\n"
        "    THEN C.trust_state := min(C.trust_state, P.trust_state)\n"
        "         Recursively for all provenance descendants.\n"
        "\n"
        "Rule T3 — Upgrades never cascade\n"
        "  IF node P.trust_state improves (moves to a higher ordinal)\n"
        "     THEN provenance children are NOT affected.\n"
        "          Re-promotion requires explicit re-derive or human adjudication.\n"
        "\n"
        "Rule T4 — Multiple parents cap to the lowest\n"
        "  IF node C has provenance parents {P1, P2, ..., Pn}\n"
        "     AND min(Pi.trust_state) = T\n"
        "    THEN C.trust_state ≤ T\n"
        "```\n"
        "\n"
        "| Parent change | Child effect |\n"
        "|---|---|\n"
        "| `auto-verified → draft` | Child capped at `draft` |\n"
        "| `human-approved → stale` | Child capped at `stale` |\n"
        "| `draft → stale` | Child capped at `stale` |\n"
        "| Any upgrade | **No cascade** |\n"
        "\n"
        "Implementation: `store.update_trust_state()`.\n"
        "\n"
        "---\n"
        "\n"
        "## 5. Contradiction propagation rules\n"
        "\n"
        "```\n"
        "Rule X1 — Contradicts edge trigger\n"
        "  Writing a 'contradicts' edge automatically opens a contestation event.\n"
        "  No other relation ('derived_from', 'related', 'refines') triggers this flow.\n"
        "\n"
        "Rule X2 — Event creation\n"
        "  On 'contradicts' write, in a single transaction:\n"
        "    1. Insert event_queue row with event_type='contradicts_edge_needs_review'\n"
        "    2. Walk provenance DAG upward from the target (find all transitive descendants)\n"
        "    3. Insert one event_node_link per descendant found\n"
        "    4. For each newly-linked node with is_contested=0:\n"
        "       is_contested := 1, contested_at := now\n"
        "    5. Set target confidence = 'low'\n"
        "    6. Recompute confidence for all descendant synthesis nodes (loop until stable)\n"
        "\n"
        "Rule X3 — Contradiction propagation is atomic\n"
        "  The entire X2 sequence shares the caller's transaction.\n"
        "  If any step fails, the whole propagation rolls back (no orphan events).\n"
        "\n"
        "Rule X4 — Multiple contradictions stack\n"
        "  Multiple 'contradicts' edges targeting the same node produce multiple events.\n"
        "  Each event is independent (its own proposal, its own decision).\n"
        "  is_contested = 1 if ANY open event covers the node.\n"
        "\n"
        "Rule X5 — Confidence cascade on contradicts (transitive)\n"
        "  When a node gets confidence='low' due to a contradicts edge,\n"
        "  all synthesis descendants recompute via Rule C5.\n"
        "  Loop converges in at most D iterations (D = graph depth).\n"
        "\n"
        "Rule X6 — No fast-path\n"
        "  Every 'contradicts' edge produces a contestation event, regardless of\n"
        "  authorship (written_by) or trust of the involved nodes.\n"
        "  There is no scenario where a contradicts edge directly sets trust_state='stale'\n"
        "  without human adjudication.\n"
        "```\n"
        "\n"
        "Implementation: `store.create_edge()`, `store._propagate_contradiction()`.\n"
        "\n"
        "---\n"
        "\n"
        "## 6. Contestation & review\n"
        "\n"
        "```\n"
        "Rule R1 — Event lifecycle\n"
        "  event_queue.status ∈ {'pending', 'closed'}\n"
        "  pending →  human adjudication (accept | reject | dismiss) → closed\n"
        "\n"
        "Rule R2 — Proposal lifecycle\n"
        "  review_proposal.status ∈ {'pending', 'accepted', 'rejected', 'dismissed'}\n"
        "  One proposal per event (UNIQUE on event_id).\n"
        "\n"
        "Rule R3 — Accept semantics\n"
        "  IF review accept:\n"
        "    1. For every node in affected_node_ids:\n"
        "       trust_state := 'stale'\n"
        "    2. Remove all event_node_link rows for this event\n"
        "    3. For each formerly-linked node:\n"
        "       IF no other open event covers it THEN is_contested := 0, contested_at := NULL\n"
        "\n"
        "Rule R4 — Reject semantics\n"
        "  IF review reject:\n"
        "    1. Remove all event_node_link rows for this event\n"
        "    2. For each formerly-linked node: recompute is_contested (same as R3.3)\n"
        "    3. trust_state is UNTOUCHED\n"
        "\n"
        "Rule R5 — Dismiss semantics\n"
        "  Same as reject (R4) but recorded as 'dismissed' for audit trail.\n"
        "\n"
        "Rule R6 — Review proposal structure\n"
        "  review_proposal.affected_node_ids = JSON array of node IDs materially dependent\n"
        "                                       on the contested claim\n"
        "  review_proposal.damage_boundary_node_id = deepest affected node (or NULL)\n"
        "  review_proposal.rationale_md = free-text Markdown explanation\n"
        "  review_proposal.confidence ∈ {'high', 'medium', 'low'}\n"
        "```\n"
        "\n"
        "Implementation: `store.accept_proposal()`, `store.reject_proposal()`, `store.dismiss_proposal()`, `store._close_contestation_event()`. CLI: `review accept|reject|dismiss` commands.\n"
        "\n"
        "---\n"
        "\n"
        "## 7. Deterministic checks (draft \u2192 auto-verified gate)\n"
        "\n"
        + sec7_intro
        + "\n"
        + "```\n"
        + "Rule D6 \u2014 Auto-verified gate\n"
        + "  A node passes from 'draft' to 'auto-verified' ONLY if all checks D1\u2013D5 pass.\n"
        + "  Failures persist as JSON in node.check_failures.\n"
        + "  Trust state is set by the CLI caller (checks.py only reports failures).\n"
        + "```\n"
        + "\n"
        + "Implementation: `checks.run_checks()` delegates to `CHECK_RULES` in `rules.py`. CLI: `_do_derive()` and `_do_synthesize()` call `update_trust_state()` with `check_failures` list.\n"
        + "\n"
        + "---\n"
        "\n"
        "## 8. Derivation structural rules\n"
        "\n"
        "```\n"
        "Rule S1 — Derivation note format\n"
        "  A derivation note MUST contain, in order:\n"
        "    1. Exactly one top-level heading: '# <title>'\n"
        "       (title MUST NOT be 'Summary' or 'Untitled')\n"
        "    2. Body prose summarising the source\n"
        "    3. Terminal '## Synthesis' section\n"
        "\n"
        "Rule S2 — Synthesis statement format (in prose)\n"
        "  Every synthesis inference MUST appear as its own line:\n"
        "    > Synthesis: <inference>\n"
        "  No bullet, number, bold, or italic markup may precede the marker.\n"
        "  The literal prefix '> Synthesis: ' (case-sensitive) is non-negotiable.\n"
        "\n"
        "Rule S3 — Synthesis statement count\n"
        "  Single-source derivation: minimum 1 statement.\n"
        "  Cross-source synthesis: minimum 1 statement.\n"
        "\n"
        "Rule S4 — Content/statements consistency\n"
        "  Every string in synthesis_statements MUST appear verbatim in prose\n"
        "  after '> Synthesis: '. Every marker line in prose MUST have a matching\n"
        "  list entry. (Prompt-level contract; not enforced by parser.)\n"
        "\n"
        "Rule S5 — Agent response envelope\n"
        "  Agents MUST return JSON: {\"prose\": \"<markdown>\", \"synthesis_statements\": [...]}\n"
        "  Falling back: if JSON parsing fails, the entire response is treated as prose\n"
        "  and synthesis statements are recovered by regex: ^>\\s*Synthesis:\\s+(.+)$\n"
        "\n"
        "Rule S6 — Adversarial validation\n"
        "  IF validator is configured (MEMEX_VALIDATOR):\n"
        "    validator checks that the derivation genuinely re-elaborates the source.\n"
        "    Generic boilerplate (\"the article discusses\", \"the author covers\") fails.\n"
        "  IF validator is absent, not callable, or throws: passes with warning.\n"
        "```\n"
        "\n"
        "Implementation: `agent._parse_derive_response()` (full function), agent system prompts, `agent.validate_derivation()`.\n"
        "\n"
        "---\n"
        "\n"
        "## 9. Agent retrieval constraints\n"
        "\n"
        "```\n"
        "Rule A1 — Agent stop condition\n"
        "  An agent may stop on a node during top-down navigation ONLY IF:\n"
        "    trust_state ∈ {'auto-verified', 'human-approved'}\n"
        "    AND is_contested = 0\n"
        "  (ADR-0004, enforced in architecture — no code check currently since\n"
        "   graph retrieval is not yet implemented.)\n"
        "\n"
        "Rule A2 — Contested nodes require descent\n"
        "  IF is_contested = 1 OR trust_state = 'draft' OR trust_state = 'stale'\n"
        "    THEN the agent MUST descend to provenance children.\n"
        "\n"
        "Rule A3 — L0 nodes are always terminal\n"
        "  Agents should never stop on L0 (raw_source) nodes — they are the bottom\n"
        "  of the abstraction ladder and contain the original content, not synthesis.\n"
        "  (ADR-0001: agent navigates top-down, stops as early as possible.)\n"
        "```\n"
        "\n"
        "---\n"
        "\n"
        "## 10. Schema constraints (DB level)\n"
        "\n"
        "```\n"
        "Constraint SC1 — Node trust_state\n"
        "  CHECK (trust_state IN ('draft','auto-verified','human-approved','stale'))\n"
        "\n"
        "Constraint SC2 — Node confidence\n"
        "  CHECK (confidence IN ('high','medium','low'))\n"
        "\n"
        "Constraint SC3 — Edge type/relation\n"
        "  CHECK (type IN ('provenance','association'))\n"
        "  CHECK (relation IN ('derived_from','related','contradicts','refines'))\n"
        "\n"
        "Constraint SC4 — Edge written_by\n"
        "  CHECK (written_by IN ('human','llm','check','system'))\n"
        "\n"
        "Constraint SC5 — Event queue\n"
        "  CHECK (event_type IN ('contradicts_edge_needs_review'))\n"
        "  CHECK (status IN ('pending','closed'))\n"
        "\n"
        "Constraint SC6 — Review proposal\n"
        "  CHECK (status IN ('pending','accepted','rejected','dismissed'))\n"
        "  CHECK (confidence IN ('high','medium','low'))\n"
        "  UNIQUE (event_id)\n"
        "\n"
        "Constraint SC7 — Source canonical key\n"
        "  UNIQUE (canonical_key)\n"
        "\n"
        "Constraint SC8 — Provenance consistency (FK)\n"
        "  edge.from_node → node.id\n"
        "  edge.to_node   → node.id\n"
        "  event_queue.edge_id        → edge.id\n"
        "  event_queue.target_node_id → node.id\n"
        "  event_node_link.event_id   → event_queue.id\n"
        "  event_node_link.node_id    → node.id\n"
        "  source.node_id             → node.id\n"
        "```\n"
        "\n"
        "---\n"
        "\n"
        "## Appendix: Rule provenance\n"
        "\n"
        "| Rule prefix | Where defined | Nature |\n"
        "|---|---|---|\n"
        "| E1–E4 | ADR-0005, `store.py` schema | Architectural + DB |\n"
        "| C1–C6 | `store.py` `compute_node_confidence()`, `_propagate_contradiction()`, `_backfill_confidence()` | Code |\n"
        "| T1–T4 | ADR-0014, `store.py` `update_trust_state()` | Architectural + Code |\n"
        "| X1–X6 | ADR-0012, `store.py` `_propagate_contradiction()` | Architectural + Code |\n"
        "| R1–R6 | ADR-0012, `store.py` `accept/reject/dismiss_proposal()`, `_close_contestation_event()` | Architectural + Code |\n"
        "| D1–D6 | ADR-0011, `checks.py` `run_checks()` | Architectural + Code |\n"
        "| S1–S6 | `agent.py` system prompts, `_parse_derive_response()`, `validate_derivation()` | Code |\n"
        "| A1–A3 | ADR-0004, ADR-0001 | Architectural (not yet enforced in code) |\n"
        "| SC1–SC8 | `store.py` `init_schema()` | DB |\n"

    )
