"""Deterministic Checks module for the draft -> auto-verified trust-state transition.

All checks are pure: no LLM calls, no network, no randomness.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from memex.rules import CHECK_RULES, MAX_CHARS, MIN_CHARS  # noqa: F401 — re-exported for backward compat


@dataclass
class CheckResult:
    """Result of running all checks on a derivation node."""

    passed: bool
    failures: list[str] = field(default_factory=list)


def run_checks(con: sqlite3.Connection, node_id: str, content_path: Path | str) -> CheckResult:
    """Run all deterministic checks for the draft -> auto-verified transition.

    Evaluates ``CHECK_RULES`` and accumulates all failures.
    See ``src/memex/rules.py`` for the rule definitions (D1–D6).

    Args:
        con:          Open SQLite connection (foreign_keys may or may not be ON).
        node_id:      The derivation node id to check.
        content_path: Path to the derivation's markdown file.

    Returns:
        CheckResult with .passed=True and .failures=[] if all checks pass,
        or .passed=False and .failures containing human-readable descriptions.
    """
    content_path = Path(content_path)

    try:
        content = content_path.read_text(encoding="utf-8")
    except OSError as exc:
        failures: list[str] = [f"Content read failed: {exc}"]
        return CheckResult(passed=False, failures=failures)

    failures: list[str] = []
    for rule in CHECK_RULES:
        failures.extend(rule.condition(con, node_id, content_path, content))

    return CheckResult(passed=len(failures) == 0, failures=failures)
