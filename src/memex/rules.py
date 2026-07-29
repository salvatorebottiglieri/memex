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
