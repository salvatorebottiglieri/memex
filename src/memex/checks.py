"""Deterministic Checks module for the draft -> auto-verified trust-state transition.

All checks are pure: no LLM calls, no network, no randomness.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

MIN_CHARS = 100
MAX_CHARS = 50_000


@dataclass
class CheckResult:
    """Result of running all checks on a derivation node."""

    passed: bool
    failures: list[str] = field(default_factory=list)


def run_checks(con: sqlite3.Connection, node_id: str, content_path: Path | str) -> CheckResult:
    """Run all deterministic checks for the draft -> auto-verified transition.

    Args:
        con:          Open SQLite connection (foreign_keys may or may not be ON).
        node_id:      The derivation node id to check.
        content_path: Path to the derivation's markdown file.

    Returns:
        CheckResult with .passed=True and .failures=[] if all checks pass,
        or .passed=False and .failures containing human-readable descriptions.
    """
    failures: list[str] = []
    content_path = Path(content_path)

    # ------------------------------------------------------------------
    # Check 1: Provenance edge resolves
    # ------------------------------------------------------------------
    edge_row = con.execute(
        """
        SELECT to_node FROM edge
        WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
        LIMIT 1
        """,
        (node_id,),
    ).fetchone()

    if edge_row is None:
        failures.append(
            f"Provenance check failed: no derived_from edge found for node {node_id}"
        )
        provenance_target = None
    else:
        provenance_target = edge_row[0]
        # Verify the target actually exists in the node table
        target_exists = con.execute(
            "SELECT id FROM node WHERE id = ?", (provenance_target,)
        ).fetchone()
        if target_exists is None:
            failures.append(
                f"Provenance check failed: derived_from target {provenance_target!r} "
                "does not exist in the node table"
            )
            provenance_target = None

    # ------------------------------------------------------------------
    # Check 2: No dangling references — provenance target must exist
    # ------------------------------------------------------------------
    if provenance_target is not None:
        target_row = con.execute(
            "SELECT id FROM node WHERE id = ?", (provenance_target,)
        ).fetchone()
        if target_row is None:
            failures.append(
                f"Dangling reference check failed: provenance target {provenance_target!r} "
                f"does not exist in the node table"
            )

    # ------------------------------------------------------------------
    # Read the derivation content (needed for checks 3 & 4)
    # ------------------------------------------------------------------
    try:
        content = content_path.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"Content read failed: {exc}")
        content = ""

    # ------------------------------------------------------------------
    # Check 3: at least one synthesis statement (read from the column, not the markdown)
    # ------------------------------------------------------------------
    # Preferred: structured field persisted by the agent (synthesis_statements JSON).
    # Fallback: the old "> Synthesis:" marker in markdown — kept for legacy nodes whose
    # row predates the column or for hand-edited files that dropped the field.
    ss_row = con.execute(
        "SELECT synthesis_statements FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    has_structured = False
    if ss_row is not None and ss_row[0]:
        try:
            import json as _json
            has_structured = bool(_json.loads(ss_row[0]))
        except _json.JSONDecodeError:
            has_structured = False
    if not has_structured and "> Synthesis:" not in content:
        failures.append(
            "Synthesis marker check failed: derivation must contain at least one "
            '"> Synthesis:" statement (or have a non-empty synthesis_statements column)'
        )

    # ------------------------------------------------------------------
    # Check 4: Size / scope bounds
    # ------------------------------------------------------------------
    length = len(content)
    if length < MIN_CHARS:
        failures.append(
            f"Size check failed: derivation is too short ({length} chars, minimum is {MIN_CHARS})"
        )
    elif length > MAX_CHARS:
        failures.append(
            f"Size check failed: derivation is too long ({length} chars, maximum is {MAX_CHARS})"
        )

    # ------------------------------------------------------------------
    # Check 5: Tier / depth consistency
    # ------------------------------------------------------------------
    row = con.execute(
        "SELECT tier, depth FROM node WHERE id = ?",
        (node_id,),
    ).fetchone()

    if row is not None:
        tier = row[0]
        depth = row[1]
        if tier is None or tier == "":
            # raw_source — must be depth 0
            if depth != 0:
                failures.append(
                    f"Tier/depth inconsistency: node has no tier but depth={depth} (expected 0)"
                )
        elif tier == "notes":
            if depth != 1:
                failures.append(
                    f"Tier/depth inconsistency: tier=notes but depth={depth} (expected 1)"
                )
        elif tier == "synthesis":
            if depth < 2:
                failures.append(
                    f"Tier/depth inconsistency: tier=synthesis but depth={depth} (expected >= 2)"
                )

    return CheckResult(passed=len(failures) == 0, failures=failures)
