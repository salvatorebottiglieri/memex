"""Vault-wide locator integrity checks (ADR-0008, ADR-0019).

Every active locator between the two stores — the SQLite graph and the
markdown content — is re-validated against the current vault:

* ``content_path`` files still exist and are regular files;
* ``derived_from`` provenance edges still target a known node;
* source rows already known to be dead surface a signal.

The module is pure: callers pass data, not a ``Store``. No SQL, no network,
no LLM — the CLI owns the reads (``_integrity_inputs``) and the human owns
the decision to freeze (``memex freeze``).
"""
from __future__ import annotations

import stat as stat_module
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FindingCode = Literal[
    "missing_content",
    "content_not_a_file",
    "content_unreadable",
    "dangling_provenance",
    "dead_source",
]


@dataclass(frozen=True)
class Finding:
    """One broken locator. Read-only: ``memex check`` only reports it."""

    node_id: str
    code: FindingCode
    detail: str


def check_integrity(
    locations: Iterable[tuple[str, str | None]],
    provenance: Iterable[tuple[str, str]],
    sources: Iterable[tuple[str, bool]],
) -> list[Finding]:
    """Return every broken locator, in a stable order.

    Args:
        locations:  ``(node_id, content_path)`` for every node.
        provenance: ``(from_node, to_node)`` for every ``derived_from`` edge.
        sources:    ``(node_id, failed)`` for every source row.
    """
    locations = list(locations)
    findings: list[Finding] = []
    findings.extend(find_missing_content(locations))
    findings.extend(find_dangling_provenance(provenance, locations))
    findings.extend(find_dead_source(sources))
    return findings


def find_missing_content(
    locations: Iterable[tuple[str, str | None]],
) -> list[Finding]:
    """Report non-null ``content_path`` values that do not resolve to a file.

    The filesystem is an external boundary: a missing path, a directory,
    and an unreadable path are classified as findings per node rather than
    raised, so one bad node cannot abort the whole vault check.
    """
    findings: list[Finding] = []
    for node_id, content_path in locations:
        if not content_path:
            continue
        path = Path(content_path)
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            findings.append(Finding(
                node_id,
                "missing_content",
                f"content_path does not exist: {content_path}",
            ))
            continue
        except OSError as exc:
            findings.append(Finding(
                node_id,
                "content_unreadable",
                f"content_path is unreadable: {content_path}: {exc}",
            ))
            continue
        if not stat_module.S_ISREG(mode):
            findings.append(Finding(
                node_id,
                "content_not_a_file",
                f"content_path is not a regular file: {content_path}",
            ))
    return findings


def find_dangling_provenance(
    provenance: Iterable[tuple[str, str]],
    locations: Iterable[tuple[str, str | None]],
) -> list[Finding]:
    """Report ``derived_from`` edges whose target node no longer exists."""
    known_ids = {node_id for node_id, _ in locations}
    findings: list[Finding] = []
    for from_node, to_node in provenance:
        if to_node not in known_ids:
            findings.append(Finding(
                from_node,
                "dangling_provenance",
                f"derived_from target {to_node} does not exist",
            ))
    return findings


def find_dead_source(sources: Iterable[tuple[str, bool]]) -> list[Finding]:
    """Report source rows already marked failed (deliberately no HTTP probe)."""
    findings: list[Finding] = []
    for node_id, failed in sources:
        if failed:
            findings.append(Finding(
                node_id,
                "dead_source",
                "source is marked failed",
            ))
    return findings
