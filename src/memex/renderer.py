"""Renderer — projects SQLite graph into markdown frontmatter (ADR-0008).

One-way, DB -> markdown. Reads every node from the Store, computes YAML
frontmatter with metadata + tags + aliases, and writes it into the node's
markdown file with the body preserved. Idempotent.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from memex.store import Store


def render(db_path: str | Path, vault_path: str | Path) -> list[dict[str, str]]:
    """Walk all nodes and render frontmatter into each node's markdown file.

    Args:
        db_path: Path to the SQLite database file.
        vault_path: Path to the vault directory containing markdown files.

    Returns:
        List of dicts with keys ``node_id`` and ``status`` ("rendered" or "skipped").
    """
    results: list[dict[str, str]] = []
    vault_path = Path(vault_path)

    with Store.open(db_path) as store:
        for node in store.list_nodes():
            node_id = node["id"]

            if not node.get("content_path"):
                results.append({"node_id": node_id, "status": "skipped", "reason": "no_content_path"})
                continue

            md_path = Path(node["content_path"])
            if not md_path.exists():
                results.append({"node_id": node_id, "status": "skipped", "reason": "file_not_found"})
                continue

            body = _extract_body(md_path)
            frontmatter = _build_frontmatter(node, body, store)
            _write_file(md_path, frontmatter, body)
            results.append({"node_id": node_id, "status": "rendered"})

    return results


def _build_frontmatter(node: dict[str, Any], body: str, store: Store) -> dict[str, Any]:
    """Construct the YAML-serializable frontmatter dict for a node."""
    fm: dict[str, Any] = {}

    # ── Common fields ────────────────────────────────────────────
    fm["id"] = node["id"]
    fm["kind"] = node["kind"]
    fm["depth"] = node["depth"]
    fm["created_at"] = node["created_at"]
    fm["confidence"] = node["confidence"]

    # ── Tags ─────────────────────────────────────────────────────
    tags = [f"kind/{node['kind']}"]
    trust_state = node.get("trust_state")
    if trust_state:
        tags.append(f"trust_state/{trust_state}")
    if node.get("is_contested"):
        tags.append("trust_state/contested")
    tier = node.get("tier")
    if tier:
        tags.append(f"tier/{tier}")
    fm["tags"] = tags

    # ── Aliases ──────────────────────────────────────────────────
    alias = _resolve_alias(node, body)
    if alias:
        fm["aliases"] = [alias]

    # ── L0-specific fields ──────────────────────────────────────
    if node.get("kind") == "raw_source":
        fm["source_url"] = node.get("source_url") or ""
        fm["title"] = node.get("title") or ""

    # ── Derivation-specific fields ───────────────────────────────
    is_derivation = node.get("kind") != "raw_source"
    if is_derivation and trust_state:
        fm["trust_state"] = trust_state
    if is_derivation and tier:
        fm["tier"] = tier
    cf = node.get("check_failures")
    if cf is not None:
        fm["check_failures"] = cf
    ss = node.get("synthesis_statements")
    if ss:
        fm["synthesis_statements"] = ss
    # ── Edge wikilinks ────────────────────────────────────────────
    # Emit `[[<target-filename>|<human-alias>]]`. Obsidian resolves wikilinks
    # by filename, so the link always points to the right file. The alias
    # (the part after `|`) is what Obsidian *displays* — the human title.
    # This form is robust to file renames inside the vault (we read the
    # current filename from the target's content_path) and renders as the
    # readable title in the source note.
    node_id = node["id"]
    edges = [
        e for e in store.list_edges(node_id=node_id)
        if e["from_node"] == node_id
    ]
    rel_groups: dict[str, list[str]] = []
    for e in edges:
        rel = e["relation"]
        target = store.get_node(e["to_node"])
        # Filename: stem of the target's content_path. Falls back to the
        # UUID as a last resort (Obsidian will then create an empty file
        # with that name — the user will notice and we can fix it).
        content_path = (target or {}).get("content_path") or ""
        if content_path:
            filename = Path(content_path).stem
        else:
            filename = e["to_node"]
        # Display alias: title (L0) or first H1 (derivation) or UUID.
        alias = _resolve_alias(target or {}, "") or e["to_node"]
        wikilink = f"[[{filename}|{alias}]]"
        rel_groups.append((rel, wikilink))

    for rel, wikilink in rel_groups:
        existing = fm.get(rel)
        if existing is None:
            fm[rel] = wikilink
        elif isinstance(existing, list):
            existing.append(wikilink)
        else:
            fm[rel] = [existing, wikilink]

    return fm


def _resolve_alias(node: dict[str, Any], body: str) -> str | None:
    """Determine the display alias for a node.

    Priority:
      1. ``title`` from the source table (L0 nodes)
      2. First ``# H1`` heading from the body
      3. ``None`` (omit field)
    """
    title = node.get("title")
    if title:
        return title

    m = re.search(r"^# (.+)$", body, re.MULTILINE)
    if m:
        return m.group(1).strip()

    return None


def _extract_body(path: Path) -> str:
    """Return the body text of a markdown file, stripping any existing frontmatter."""
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        # Frontmatter delimited by --- ... ---
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            return parts[2].lstrip("\n")
        # If no closing ---, treat everything as body
        return parts[-1] if parts else text
    return text


def _write_file(path: Path, fm: dict[str, Any], body: str) -> None:
    """Write YAML frontmatter + body to a markdown file."""
    fm_text = yaml.dump(fm, default_flow_style=False, sort_keys=False, allow_unicode=True, width=999)
    content = f"---\n{fm_text}---\n\n{body}"
    path.write_text(content, encoding="utf-8")
