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

from memex.utils.parsing import _FENCE_RE, parse_synthesis_statements


# ── Size bounds ────────────────────────────────────────────────────

MIN_CHARS = 100
MAX_CHARS = 50_000
# Synthesis nodes rest on many parents; allow triple the notes-tier ceiling.
_SYNTH_MAX_CHARS = 150_000


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


def _node_kind(con: sqlite3.Connection, node_id: str) -> str | None:
    """Kind of *node_id*, or None when the row is missing."""
    row = con.execute("SELECT kind FROM node WHERE id = ?", (node_id,)).fetchone()
    return row[0] if row is not None else None


# ── Synthesis marker grammar (canonicalizer + D3 share it) ─────────────
#
# A synthesis marker is a LINE-ANCHORED ``> Synthesis:`` line — the file
# structure the pipeline guarantees. The canonicalizer rewrites exactly this
# grammar, and the D3 check counts exactly this grammar, so both must use
# the SAME regex or the file-vs-column comparison drifts (ticket #143).

_SYNTHESIS_MARKER_RE = re.compile(r"^>\s*Synthesis:\s*(.*)$", re.M)


def canonicalize_synthesis_markers(prose: str, statements: list[str]) -> str:
    """Rewrite the ``> Synthesis:`` marker lines of *prose* from *statements*.

    The file is the presentation channel; the ``synthesis_statements`` column
    is the source of truth the D3 check compares the file against (CONTEXT.md:
    "the column is the source of truth that the deterministic check ... all
    marker is presentation"). Agents often render the same statements with
    different quoting/style, failing D3's exact file-vs-column comparison
    (~60% of derivations went draft, ticket #143). This rewrites the file's
    markers from the (cleaned) column:

      - markers present: replaced in order; markers beyond ``len(statements)``
        are dropped;
      - no markers but statements exist: the missing statements are emitted
        as ``> Synthesis:`` lines — after an existing ``## Synthesis`` header
        when prose already carries one, else in an appended ``## Synthesis``
        section (Rule S1 format);
      - no statements: prose returned untouched (D3 failure unchanged).

    Only LINE-ANCHORED markers count (``> Synthesis:`` at the start of a
    line — the grammar ``_SYNTHESIS_MARKER_RE`` shares with the D3 check).
    The result carries EXACTLY ``len(statements)`` line-anchored markers,
    each equal to the corresponding statement.
    """
    if not statements:
        return prose

    lines = prose.splitlines()
    out: list[str] = []
    placed = 0
    for line in lines:
        if _SYNTHESIS_MARKER_RE.match(line):
            if placed < len(statements):
                out.append(f"> Synthesis: {statements[placed]}")
                placed += 1
            # marker beyond len(statements): dropped
        else:
            out.append(line)

    if placed < len(statements):
        remaining = statements[placed:]
        try:
            header = out.index("## Synthesis")
        except ValueError:
            out.append("")
            out.append("## Synthesis")
            out.extend(f"> Synthesis: {s}" for s in remaining)
        else:
            # Prose already carries a "## Synthesis" header (the agent emitted
            # it without markers): insert the missing markers immediately
            # after it instead of appending a second section.
            out[header + 1 : header + 1] = [f"> Synthesis: {s}" for s in remaining]

    text = "\n".join(out)
    if prose.endswith("\n"):
        text += "\n"
    return text


def _d3_synthesis_check(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D3: At least one synthesis statement (from DB column or file marker).

    Only derivations are gated: extracted L0s carry raw source content,
    which has no synthesis markers by construction (ticket #138). The check
    message itself says 'derivation must contain' — synthesis statements are
    a derivation-level contract, not an L0 one.
    """
    if _node_kind(con, node_id) == "extracted":
        return []

    ss_row = con.execute(
        "SELECT synthesis_statements FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    # Same parse the validation DAG applies to the column
    # (``parse_synthesis_statements``): JSON array of strings, garbage or a
    # non-list payload means no statements, never a crash.
    db_statements = (
        parse_synthesis_statements(ss_row[0]) if ss_row is not None else []
    )

    file_statements = _SYNTHESIS_MARKER_RE.findall(content)

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
    """D4: Content length between MIN_CHARS and MAX_CHARS.

    The MIN floor applies everywhere — content must be real. The MAX cap is a
    derivations-tier gate (synthesis gets a wider ceiling because it
    aggregates several parents); extracted L0s have no cap at all: raw
    content is what it is, and legitimately long sources (arXiv papers,
    hour-long transcripts) must not be draft 'too long' forever (ticket #139).
    """
    row = con.execute(
        "SELECT tier, kind FROM node WHERE id = ?", (node_id,)
    ).fetchone()
    tier, kind = (row[0], row[1]) if row is not None else (None, None)
    length = len(content)
    if length < MIN_CHARS:
        return [
            f"Size check failed: content is too short ({length} chars, minimum is {MIN_CHARS})"
        ]
    if kind == "extracted":
        return []
    max_chars = _SYNTH_MAX_CHARS if tier == "synthesis" else MAX_CHARS
    if length > max_chars:
        return [
            f"Size check failed: content is too long ({length} chars, maximum is {max_chars})"
        ]
    return []


def _d5_tier_depth(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D5: Tier/depth consistency: L0=0, notes=parent depth+1, synthesis>=2."""
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
        # Notes nodes sit one level below their parents: expected depth is
        # max(parent depth) + 1. Extracted roots are at depth 1 (notes land
        # at depth 2); legacy raw_source L0s are at depth 0 (notes land at
        # depth 1). A parentless notes node already fails D1's provenance
        # gate, so fall back to the L0-ish expectation (0) instead of
        # contradicting D1's verdict.
        parent_edges = con.execute(
            """
            SELECT to_node FROM edge
            WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
            """,
            (node_id,),
        ).fetchall()
        parent_depths: list[int] = []
        for (parent_id,) in parent_edges:
            p_row = con.execute(
                "SELECT depth FROM node WHERE id = ?", (parent_id,)
            ).fetchone()
            if p_row is not None:
                parent_depths.append(p_row[0])
        expected = max(parent_depths) + 1 if parent_depths else 0
        if depth != expected:
            return [
                f"Tier/depth inconsistency: tier=notes but depth={depth} (expected {expected})"
            ]
    elif tier == "synthesis":
        if depth < 2:
            return [
                f"Tier/depth inconsistency: tier=synthesis but depth={depth} (expected >= 2)"
            ]

    return []


# ── Inline wikilinks (D6 + V1 share the grammar) ────────────────────

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def _strip_frontmatter(text: str) -> str:
    """Return *text* without a leading YAML frontmatter block."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2]
    return text


# Markdown list-item marker: a bullet (-, *, +) or a numbered marker (1.
# 1)) followed by whitespace, allowing leading indent. Shared by the V1
# prose filter and the fenced/indented-code stripper — 4-space-indented
# NESTED list items are list continuation, not indented code.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _strip_fenced_blocks(text: str) -> str:
    """Return *text* with fenced and indented code regions removed.

    Shared by D6 and the V1 slicer so both parse the same link surface: a
    ``[[...]]`` inside a code example is code, not a wikilink declaration.
    A line whose stripped form starts with a fence (````` or ``~~~``)
    toggles the fence state; runs of indented lines (4+ literal spaces or a
    leading tab — Markdown's expanded-tab indented code blocks) are dropped
    too — blank lines inside the run keep it open, the first non-blank,
    non-indented line ends it. An indented line whose stripped form is a
    list item (``-``/``*``/``+`` or ``N.``/``N)``) is a NESTED list item,
    not indented code, so it is kept — but only OUTSIDE an open indented
    run: a list-formatted line inside a genuine code block is code, not a
    nested item, and stays dropped (the same list handling the V1 prose
    filter applies). Note that a TOP-LEVEL ``    - item`` line (a
    list-formatted line with no enclosing list and no open run) is kept in
    the surface — CommonMark would parse it as indented code, an accepted
    divergence, since a true nested list item only ever follows a
    non-indented list line. An unclosed fence drops the remainder — the
    safe direction: trailing code must never leak into the parsed surface.
    """
    lines: list[str] = []
    in_fence = False
    in_indented = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith(("    ", "\t")):
            if in_indented or not _LIST_ITEM_RE.match(stripped):
                # Indented code: inside an open run every indented line —
                # list-formatted or not — is code and keeps the run open;
                # outside a run, a non-list indented line opens the run.
                in_indented = True
                continue
            # Outside a run, a list-formatted indented line is a nested
            # list item (list continuation), not code — keep it.
        if in_indented:
            if not stripped:
                continue  # blank line inside an indented code run
            in_indented = False
        lines.append(line)
    return "\n".join(lines)


def _d6_link_validity_check(
    con: sqlite3.Connection, node_id: str, content_path: Path, content: str
) -> list[str]:
    """D6: Inline wikilinks in syntheses MUST resolve to provenance parents.

    Syntheses only: notes and extracted L0s carry no link contract (a note
    has a single parent and needs no declaration). Every ``[[filename|alias]]``
    in the body must name a node in ``derived_from`` whose content_path
    basename matches the link filename — the same resolution the renderer
    uses when it emits provenance wikilinks (``Path.stem``). Dangling or
    wrong-target links fail; a link-free synthesis passes (V1 then judges
    whether source facts were left undeclared). Frontmatter (rendered
    provenance wikilinks) and code blocks — fenced and 4-space-indented
    (code examples are not link surface — same stripping the V1 slicer
    applies) — are excluded from the parse.
    """
    if _node_kind(con, node_id) == "extracted":
        return []
    row = con.execute("SELECT tier FROM node WHERE id = ?", (node_id,)).fetchone()
    if row is None or row[0] != "synthesis":
        return []  # notes and legacy L0s are exempt from the link contract

    links = _WIKILINK_RE.findall(
        _strip_fenced_blocks(_strip_frontmatter(content))
    )
    if not links:
        return []

    parent_rows = con.execute(
        """
        SELECT to_node FROM edge
        WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
        """,
        (node_id,),
    ).fetchall()
    valid: set[str] = set()
    for (pid,) in parent_rows:
        prow = con.execute(
            "SELECT content_path FROM node WHERE id = ?", (pid,)
        ).fetchone()
        if prow is not None and prow[0]:
            valid.add(Path(prow[0]).stem)
        else:
            # A parent without a content file has no stem to link to; the
            # synthesize link-target fallback emits its node id instead
            # (``Path(content_path).stem if content_path else parent["id"]``),
            # so D6 must accept that id as a valid name too.
            valid.add(pid)

    failures: list[str] = []
    for filename, alias in links:
        if filename not in valid:
            label = f"[[{filename}|{alias}]]" if alias else f"[[{filename}]]"
            failures.append(
                f"{SEVERITY_FATAL} Link validity check failed: {label} does not "
                f"resolve to a provenance parent of {node_id}"
            )
    return failures


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
        description="Tier/depth consistency: L0=0, notes=parent depth+1, synthesis>=2",
        condition=_d5_tier_depth,
        consequence="Tier/depth check",
    ),
    Rule(
        id="D6",
        category="check",
        description="Inline wikilinks in syntheses resolve to provenance parents",
        condition=_d6_link_validity_check,
        consequence="Link validity check",
    ),
]

# ── Adversarial validation rules (V1–V2, LLM-judged) ────────────────
#
# A family of small, orthogonal criteria symmetric to the deterministic
# checks. They run POST-creation (the node and its provenance edges exist),
# so evidence is the node's own content plus its parents' contents (read
# from the parents' content_path files). Every verdict is structured and
# cites the claim plus a supporting/refuting excerpt. A judge call or
# verdict-parse failure degrades to pass-with-warning — it never crashes
# the derive.

# Severity tags carried by every gate failure message. Fatal (D6, D7,
# V1-UNSUPPORTED): one is enough → draft. Quality (V2): draft annotated
# severity=quality, human-promotable. The tags are an informational
# annotation guiding human review: both severities gate to draft (there is
# no separate quality_failed state) and draft nodes are human-promotable via
# the existing review flow. auto-verified ⇒ every unadorned claim is
# grounded.
SEVERITY_FATAL = "[severity=fatal]"
SEVERITY_QUALITY = "[severity=quality]"


@dataclass(frozen=True)
class ValidationRule:
    """A single LLM-judged validation criterion.

    - ``slicer(node_content, node, parents) -> list[str]``: slice the node's
      evidence into rendered prompt blocks. ``node`` is a dict with ``tier``,
      ``kind`` and ``synthesis_statements``; ``parents`` is a list of dicts
      with ``node_id``, ``filename``, ``content_path``, ``content`` (None
      when unreadable) and ``title``. Return [] to skip the rule (nothing
      to judge).

    - ``prompt_template``: str with ``{context}``, ``{slices}``, ``{body}``,
      ``{parents}`` and (V2 only) ``{v1_verdicts}`` placeholders (unused
      ones are left untouched).

    - ``verdict_parser(raw, payload) -> (failures, warning, verdicts)``:
      parse the judge's structured verdict (host-tool payload wins over
      JSON-in-text); return failure messages (empty = pass), an optional
      warning string when the verdict was unusable, and the normalized
      structured verdicts (consumed by downstream DAG members, e.g. D7
      verifies V1's evidence quotes).

    DAG placement fields (``run_validations`` executes waves in ascending
    ``order``; nothing else in the registry is hardcoded):

    - ``order``: ascending execution order.
    - ``depends_on``: rule ids whose waves must have run first.
    - ``skip_when_fatal``: skip this wave when a ``depends_on`` wave
      produced a ``[severity=fatal]`` failure (V2 is skipped when V1 is
      fatal — the node is draft already, the judge call is saved).
    - ``expects_full_verdicts``: one verdict per presented slice; a
      shortfall (fewer verdicts than slices, including an empty set) emits
      a warning so an incomplete grounding pass is never silently clean.
    """

    id: str
    description: str
    slicer: Callable[..., list[str]]
    prompt_template: str
    verdict_parser: Callable[..., tuple[list[str], str | None, list[dict[str, Any]]]]
    order: int = 0
    depends_on: tuple[str, ...] = ()
    skip_when_fatal: bool = False
    expects_full_verdicts: bool = False


def _parse_json_verdict(raw: str) -> dict[str, Any] | None:
    """Best-effort parse of a JSON object, tolerating markdown code fences."""
    stripped = _FENCE_RE.sub("", (raw or "").strip())
    try:
        data = json.loads(stripped)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


# ── V1 — evidence support ────────────────────────────────────────────

# Claims: sentence-split unadorned body prose. Sentence boundaries are
# masked out of decimals ("3.14"), common abbreviations ("Dr.", "e.g.",
# "U.S."), initials ("J. R. R.") and ellipses before splitting.
_DOT = "\u0000"
_ELLIPSIS_RE = re.compile(r"\.\.\.")
_DECIMAL_RE = re.compile(r"(\d)\.(\d)")
_ABBR_TOKEN_RE = re.compile(
    r"\b((?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|approx|dept|fig|inc|ltd|co|"
    r"vol|pp|ed|eds|trans|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|"
    r"e\.g|i\.e|U\.S|U\.K|a\.m|p\.m))\.(?=\s|$)",
    re.I,
)
_INITIALS_RE = re.compile(r"\b([A-Z])\.(?=\s+[A-Z]\.)")
_INITIAL_NAME_RE = re.compile(r"\b([A-Z])\.(?=\s+[A-Z][a-z]+)")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


def _mask_sentence_endings(text: str) -> str:
    masked = _ELLIPSIS_RE.sub(_DOT * 3, text)
    masked = _DECIMAL_RE.sub(rf"\1{_DOT}\2", masked)
    masked = _ABBR_TOKEN_RE.sub(rf"\1{_DOT}", masked)
    masked = _INITIALS_RE.sub(rf"\1{_DOT}", masked)
    masked = _INITIAL_NAME_RE.sub(rf"\1{_DOT}", masked)
    return masked


def _split_claims(prose: str) -> list[str]:
    """Split *prose* into sentence-level claims (abbreviation/decimals safe)."""
    masked = _mask_sentence_endings(prose)
    parts = _SENT_SPLIT_RE.split(masked)
    claims = [p.replace(_DOT, ".").strip() for p in parts]
    return [c for c in claims if c]


# Markdown table separator row without a leading pipe (e.g. ``---|---``);
# rows with a leading pipe are caught by the startswith("|") check.
_TABLE_SEP_RE = re.compile(r"^\s*:?-{2,}(?:\s*\|[\s:|-]*)*$")


def _unadorned_prose(content: str) -> str:
    """Body prose minus frontmatter, headings, ``> Synthesis:`` markers,
    fenced code blocks, tables, and list/blockquote lines.

    Only unadorned prose sentences become V1 claims: a code example or a
    table cell is not a claim the judge can ground against parent content,
    and bullets/blockquotes are presentation. Fenced and indented code
    regions (4-space or tab-indented) are dropped entirely (an unclosed
    fence drops the remainder — the safe direction: trailing code must not
    leak into the claims).
    """
    text = _strip_fenced_blocks(_strip_frontmatter(content))
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if _SYNTHESIS_MARKER_RE.match(stripped):
            continue
        if stripped.startswith((">", "|")) or _LIST_ITEM_RE.match(stripped):
            continue
        if _TABLE_SEP_RE.match(stripped):
            continue
        lines.append(stripped)
    return " ".join(lines)


def _v1_evidence_slicer(
    content: str, node: dict[str, Any], parents: list[dict[str, Any]]
) -> list[str]:
    """Split the unadorned body prose into sentence-level claims."""
    claims = _split_claims(_unadorned_prose(content))
    if not claims:
        return []
    blocks: list[str] = []
    for i, claim in enumerate(claims, start=1):
        block = f'Claim {i}: "{claim}"'
        links = _WIKILINK_RE.findall(claim)
        if links:
            resolved: list[str] = []
            for filename, alias in links:
                label = f"[[{filename}|{alias}]]" if alias else f"[[{filename}]]"
                if any(p["filename"] == filename for p in parents):
                    resolved.append(f"{label} -> parent {filename}")
                else:
                    resolved.append(f"{label} -> NOT a provenance parent")
            block += "\n  links: " + "; ".join(resolved)
        blocks.append(block)
    return blocks


_V1_PROMPT_TEMPLATE = """\
You are a strict evidence auditor for a knowledge-graph system. A derivation
node was just created from one or more source documents (its "parents"). Your
job: judge every unadorned claim in the node's body against the parent content.

{context}

Judge each claim below and submit one verdict per claim:
  - SUPPORTED: the relevant parent content states the claim, or directly
    implies it. evidence_quote MUST be a verbatim quote from that parent
    content (it is verified deterministically).
  - COMMON_KNOWLEDGE: the claim is a generic, uncontroversial fact that needs
    no source. Quantitative claims about specific entities are NEVER exempt
    from evidence — never mark those COMMON_KNOWLEDGE.
  - UNSUPPORTED: the claim is not supported by the relevant parent content.
    You MUST cite the source examined and why the source does not contain it.

Rules:
- In a synthesis (node tier = synthesis), every fact taken from a source MUST
  carry an inline link [[filename|alias]] naming the parent it comes from.
  A source-derived fact WITHOUT such a link is UNSUPPORTED (missing
  declaration).
- A claim WITH a link is judged ONLY against the parent the link names — the
  parent contents are listed in the Parent content section, keyed by
  filename.
- In a notes derivation, claims are judged against the single parent
  regardless of any inline links they carry.

Submit your verdicts by calling the submit_verdicts tool with a JSON payload:
{"verdicts": [
  {"claim": "<claim text>", "verdict": "SUPPORTED", "evidence_quote": "<verbatim quote from the cited source>"},
  {"claim": "<claim text>", "verdict": "COMMON_KNOWLEDGE", "evidence_quote": ""},
  {"claim": "<claim text>", "verdict": "UNSUPPORTED", "source_examined": "<parent filename examined>", "absence_explanation": "<why the source does not contain the claim>"}
]}
If the submit_verdicts tool is unavailable, return ONLY that JSON object —
no commentary.

{slices}

{parents}
"""


def _v1_verdict_parser(
    raw: str, payload: dict[str, Any] | None
) -> tuple[list[str], str | None, list[dict[str, Any]]]:
    """V1 verdicts: per-claim SUPPORTED / COMMON_KNOWLEDGE / UNSUPPORTED.

    Returns (failures, warning, verdicts) — the normalized verdicts feed the
    downstream DAG members (D7 quote verification, V2's grounding block).
    """
    data = payload if isinstance(payload, dict) else _parse_json_verdict(raw)
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        return [], "V1 response parse failed, validation skipped", []
    failures: list[str] = []
    verdicts: list[dict[str, Any]] = []
    for v in data["verdicts"]:
        if not isinstance(v, dict):
            continue
        claim = v.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            continue
        verdict = str(v.get("verdict", "")).upper()
        if verdict not in ("SUPPORTED", "COMMON_KNOWLEDGE", "UNSUPPORTED"):
            continue
        normalized: dict[str, Any] = {"claim": claim.strip(), "verdict": verdict}
        if verdict == "SUPPORTED":
            quote = v.get("evidence_quote")
            normalized["evidence_quote"] = quote if isinstance(quote, str) else ""
        elif verdict == "UNSUPPORTED":
            source = v.get("source_examined")
            explanation = v.get("absence_explanation")
            normalized["source_examined"] = (
                source if isinstance(source, str) else ""
            )
            normalized["absence_explanation"] = (
                explanation if isinstance(explanation, str) else ""
            )
            message = f"{SEVERITY_FATAL} Unsupported claim: {claim.strip()}"
            if normalized["source_examined"]:
                message += f" (source_examined: {normalized['source_examined']})"
            if normalized["absence_explanation"]:
                message += (
                    f" (absence_explanation: {normalized['absence_explanation']})"
                )
            # Negative-verdict contract: an UNSUPPORTED verdict MUST cite the
            # source examined and why the source lacks the claim. A judge
            # omitting either field violates the contract — deterministic
            # failure, symmetric to D7's SUPPORTED-without-quote treatment.
            if not normalized["source_examined"] or not normalized[
                "absence_explanation"
            ]:
                message += (
                    " [negative-verdict contract violated: UNSUPPORTED must "
                    "cite source_examined and absence_explanation]"
                )
            failures.append(message)
        verdicts.append(normalized)
    return failures, None, verdicts


# ── V2 — re-elaboration quality ──────────────────────────────────────

def _v2_evidence_slicer(
    content: str, node: dict[str, Any], parents: list[dict[str, Any]]
) -> list[str]:
    """Synthesis statements (from the node column; file markers as fallback)."""
    statements = node.get("synthesis_statements") or []
    if statements:
        stmts = [str(s) for s in statements if str(s).strip()]
    else:
        stmts = _SYNTHESIS_MARKER_RE.findall(content)
    return [f'Statement {i}: "{s}"' for i, s in enumerate(stmts, start=1)]


_V2_PROMPT_TEMPLATE = """\
You are a strict re-elaboration auditor for a knowledge-graph system. A
derivation node aggregates facts from its parents; its synthesis statements
must be legitimate, specific inferences that go beyond the source material.

{context}

Evaluate the synthesis statements below and submit ONE verdict:
  - passes = true: the statements are legitimate, specific inferences that
    go beyond the source and follow from the body's facts.
  - passes = false: a statement is boilerplate ("the article discusses",
    "the author covers"), vacuous, or not a legitimate inference from the
    body's facts.

Note: factual grounding is judged by a separate evidence criterion (V1) —
its per-claim verdicts are listed below. Do NOT re-verify whether the
body's facts are true; judge only whether the statements are legitimate,
specific re-elaborations of those facts as V1 saw them.

Submit your verdict by calling the submit_verdicts tool with a JSON payload:
{"passes": true, "reason": "<explanation>"}
If the submit_verdicts tool is unavailable, return ONLY that JSON object —
no commentary.

Synthesis statements:
{slices}

Node body:
{body}

V1 verdicts (grounding — the body as V1 judged it):
{v1_verdicts}

{parents}
"""


def _v2_verdict_parser(
    raw: str, payload: dict[str, Any] | None
) -> tuple[list[str], str | None, list[dict[str, Any]]]:
    """V2 verdict: a single {passes, reason}; quality-level severity.

    ``passes`` is coerced bool-ish (mirroring V1's lenient verdict
    normalization): 'true', '1', 'yes', 'on' — strings or numbers — all
    count as passing, so a judge's JSON sloppiness never downgrades a good
    node to draft.
    """
    data = payload if isinstance(payload, dict) else _parse_json_verdict(raw)
    if not isinstance(data, dict) or "passes" not in data:
        return [], "V2 response parse failed, validation skipped", []
    passes_value = data.get("passes", "")
    if isinstance(passes_value, bool):
        passing = passes_value
    elif isinstance(passes_value, (int, float)):
        # JSON-decodable numbers: 1 / 1.0 -> pass, 0 / 0.0 -> fail.
        passing = passes_value != 0
    else:
        passing = str(passes_value).strip().upper() in ("TRUE", "1", "YES", "ON")
    if passing:
        return [], None, []
    reason = data.get("reason")
    if isinstance(reason, str) and reason.strip():
        return [
            f"{SEVERITY_QUALITY} Re-elaboration quality failed: {reason.strip()}"
        ], None, []
    return [
        f"{SEVERITY_QUALITY} Re-elaboration quality failed: synthesis statement "
        "is boilerplate or unsupported"
    ], None, []


VALIDATION_RULES: list[ValidationRule] = [
    ValidationRule(
        id="V1",
        description=(
            "Evidence support: every unadorned claim is SUPPORTED / "
            "COMMON_KNOWLEDGE / UNSUPPORTED against the parent content"
        ),
        slicer=_v1_evidence_slicer,
        prompt_template=_V1_PROMPT_TEMPLATE,
        verdict_parser=_v1_verdict_parser,
        # DAG root: runs first, one verdict per claim, no dependencies.
        order=1,
        expects_full_verdicts=True,
    ),
    ValidationRule(
        id="V2",
        description=(
            "Re-elaboration quality: synthesis statements go beyond the "
            "source, are specific, and follow from the body's facts"
        ),
        slicer=_v2_evidence_slicer,
        prompt_template=_V2_PROMPT_TEMPLATE,
        verdict_parser=_v2_verdict_parser,
        # Runs after V1 (and D7, which verifies V1's quotes inside V1's
        # wave); skipped when V1 produced fatal failures — the node is
        # draft already and the judge call is saved.
        order=2,
        depends_on=("V1",),
        skip_when_fatal=True,
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
        "| `kind` | enum | 1 | `url` | `extracted` | `summary` | (open vocabulary) |\n"
        "| `tier` | enum | null | 0–1 | `raw` | `notes` | `synthesis` (fixed spine, ADR-0002). Null = L0 |\n"
        "| `trust_state` | enum | 1 | `draft` > `auto-verified` > `human-approved` > `stale` (strict ordinal) |\n"
        "| `depth` | int | 1 | Computed: `max(parent.depth) + 1`. L0 = 0 |\n"
        "| `confidence` | enum | 1 | `high` > `medium` > `low`. Inherited from parents |\n"
        "| `is_contested` | bool | 1 | Orthogonal to trust_state. `1` if any open `event_node_link` exists |\n"
        "| `contested_at` | ISO-8601 | null | 0–1 | When first contested |\n"
        "| `content_path` | path | 1 | Filesystem path to markdown file |\n"
        "| `synthesis_statements` | JSON array | null | 0–1 | Structured list of inferences |\n"
        "| `check_failures` | JSON array | null | 0–1 | Deterministic (D1–D6) + adversarial (V1–V2) gate failures (if any) |\n"
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
        + "Rule D0 \u2014 Auto-verified gate\n"
        + "  A node passes from 'draft' to 'auto-verified' ONLY if all checks D1\u2013D6 pass\n"
        + "  AND the validation DAG passes: V1 (grounding) \u2192 D7 (quote\n"
        + "  verification over V1's verdicts) \u2192 V2 (re-elaboration quality,\n"
        + "  consuming V1's verdicts; skipped when V1 is fatal).\n"
        + "  MEMEX_VALIDATION=off disables the DAG (deterministic D1\u2013D7 never opt\n"
        + "  out \u2014 D7 runs over V1's verdicts and is vacuous without them).\n"
        + "  Failures accumulate in node.check_failures, each identifying its\n"
        + "  criterion (id prefix \u2014 'D7: ', 'V1: ', 'V2: ' \u2014 or the\n"
        + "  consequence label for the D-checks, e.g. 'Link validity check\n"
        + "  failed'); the gate failures D6, D7, V1 and V2 carry two-level\n"
        + "  severity tags: fatal (D6, D7, V1-UNSUPPORTED) \u2014 one is\n"
        + "  enough \u2192 draft; quality (V2) \u2192 draft, human-promotable. The\n"
        + "  tag is an informational annotation guiding human review: both\n"
        + "  severities gate to draft (there is no separate quality_failed\n"
        + "  state) and draft nodes are human-promotable via the existing\n"
        + "  review flow.\n"
        + "  Invariant: auto-verified \u21d2 every unadorned claim is grounded.\n"
        + "  Trust state is set by the CLI caller (checks.py only reports failures).\n"
        + "```\n"
        + "\n"
        + "Implementation: `checks.run_checks()` delegates to `CHECK_RULES`; `validators.validate.run_validations()` runs the validation DAG (V1 \u2192 D7 \u2192 V2) from `VALIDATION_RULES` with the judge agent (`MEMEX_JUDGE`, default = the derive agent). services/derive.py and services/synthesize.py merge both failure lists (`merge_gate_failures`) and call `update_trust_state()`.\n"
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
        "Rule S6 \u2014 Factual fidelity (prompt contract)\n"
        "  Statistics or specific numbers absent from the source MUST be omitted\n"
        "  or marked as synthesis statements \u2014 never invented, rounded, or\n"
        "  approximated from memory.\n"
        "  In syntheses, every source-derived fact MUST carry an inline wikilink\n"
        "  [[filename|alias]] naming the parent it comes from.\n"
        "\n"
        "Rule V1 \u2014 Evidence support (LLM-judged, DAG root)\n"
        "  Every unadorned claim in the body is judged SUPPORTED /\n"
        "  COMMON_KNOWLEDGE / UNSUPPORTED against the parent content, with an\n"
        "  evidence quote. COMMON_KNOWLEDGE covers generic uncontroversial\n"
        "  facts only; quantitative claims about specific entities are NEVER\n"
        "  exempt. In syntheses, a source-derived fact WITHOUT a link is\n"
        "  UNSUPPORTED (missing declaration); a claim WITH a link is judged\n"
        "  against the linked parent only. In notes, every claim is judged\n"
        "  against the single parent regardless of any inline links it\n"
        "  carries (the notes exemption — a stray wikilink in note prose\n"
        "  never redirects the judgment to a parent that does not exist).\n"
        + "  Negative-verdict contract: every UNSUPPORTED verdict cites the\n"
        + "  claim, the source examined, and why the source does not contain it\n"
        + "  (source_examined, absence_explanation). An UNSUPPORTED verdict\n"
        + "  lacking either field produces a deterministic contract-violation\n"
        + "  failure (symmetric to D7's SUPPORTED-without-quote failure). A\n"
        + "  verdict shortfall (a presented claim with no matching verdict,\n"
        + "  including an empty set) emits a warning \u2014 coverage is\n"
        + "  correlated per presented claim INSTANCE (whitespace-normalized\n"
        + "  claim text, inline link markers ignored): N identical presented\n"
        + "  claims need N verdicts, so a single verdict for a duplicated\n"
        + "  sentence leaves its twin unjudged, and an echo that drops or\n"
        + "  adds [[...]] markers still resolves to the presented claim.\n"
        + "  Duplicate verdicts or verdicts whose claim was never presented\n"
        + "  are coverage gaps, never a clean pass; grounding coverage is\n"
        + "  then incomplete, never silently clean.\n"
        "\n"
        + "Rule D7 \u2014 Evidence-quote verification (deterministic, over V1's output)\n"
        + "  Every evidence_quote V1 cites for a SUPPORTED verdict must appear\n"
        + "  in the cited source (linked parent for syntheses, single parent\n"
        + "  for notes). Matching is a literal substring with a\n"
        + "  whitespace-collapsed fallback: quote and source are compared with\n"
        + "  every run of whitespace collapsed to a single space (LLMs re-wrap\n"
        + "  line breaks; a fabricated quote differs in words, not whitespace).\n"
        + "  Quote not found \u2192 failure D7. This keeps\n"
        + "  LLM-judged evidence honest: an LLM that hallucinates a claim can\n"
        + "  hallucinate the supporting quote. COMMON_KNOWLEDGE is backstopped\n"
        + "  too: a COMMON_KNOWLEDGE verdict on a link-free synthesis claim is\n"
        + "  a missing declaration (a source-derived fact without an inline\n"
        + "  link is UNSUPPORTED) and fails deterministically. The cited\n"
        + "  source (and the COMMON_KNOWLEDGE link check) resolves from the\n"
        + "  PRESENTED claim text, not the verdict's echo: each verdict is\n"
        + "  correlated to the presented slice (whitespace-normalized, link\n"
        + "  markers ignored \u2014 LLMs routinely echo/normalize claim text,\n"
        + "  dropping or adding [[...]] markers); only a verdict matching no\n"
        + "  slice falls back to the echoed claim.\n"
        "\n"
        "Rule V2 \u2014 Re-elaboration quality (LLM-judged, consumes V1's verdicts)\n"
        "  Synthesis statements must be legitimate inferences from the body's\n"
        "  facts, specific, and go beyond the source; boilerplate fails.\n"
        "  Grounding is V1's job \u2014 V2 does not re-verify the facts; its prompt\n"
        "  carries V1's per-claim verdicts (the body as V1 saw it).\n"
        "\n"
        + "DAG execution: waves run in ascending registry order (V1 first; D7\n"
        + "  verifies V1's quotes inside V1's wave; V2 declares\n"
        + "  depends_on=(\"V1\",) + skip_when_fatal, so it runs after D7 and is\n"
        + "  SKIPPED when V1 has fatal failures \u2014 the node is draft already,\n"
        + "  re-derive re-runs both). The DAG is declarative: VALIDATION_RULES\n"
        + "  carries order/depends_on/skip_when_fatal/expects_full_verdicts;\n"
        + "  adding a criterion never edits run_validations.\n"
        + "  MEMEX_VALIDATION=off disables the DAG (deterministic checks never\n"
        + "  opt out). Judge call/parse failures degrade to pass-with-warning;\n"
        + "  V1 verdict shortfalls warn.\n"
        "\n"
        "Rule V3 \u2014 Registry curation (process, not a criterion)\n"
        "  VALIDATION_RULES has an entry/exit process: every new member adds\n"
        "  ONE disjoint defect class with a declared scope (what it catches,\n"
        "  what it does not), placed in the DAG; members exit when their\n"
        "  defect class is subsumed. Without curation the family degrades into\n"
        "  N overlapping mini-judges.\n"
        "```\n"
        "\n"
        "Implementation: agent system prompts (factual fidelity), `validators.validate.run_validations()` + `VALIDATION_RULES` in `rules.py` (V1\u2013V2) with D7 quote verification in `validate.py`. Judge = `MEMEX_JUDGE` or the derive agent; `submit_verdicts` host tool (pi.py) carries structured verdicts.\n"
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
        "  Agents should never stop on L0 nodes — the L0 is the URL root plus\n"
        "  its extracted content node, the bottom of the abstraction ladder,\n"
        "  carrying the original content, not synthesis. (ADR-0001: agent\n"
        "  navigates top-down, stops as early as possible.)\n"
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
        "| D7 | `validators/validate.py` (deterministic, over V1's verdicts) | Code |\n"
        "| S1–S6 | agent system prompts, `_parse_derive_response()` | Code |\n"
        "| V1–V2 | `validators/validate.py` `run_validations()`, `rules.py` `VALIDATION_RULES` (curated: one disjoint defect class per member, see Rule V3) | Code |\n"
        "| A1–A3 | ADR-0004, ADR-0001 | Architectural (not yet enforced in code) |\n"
        "| SC1–SC8 | `store.py` `init_schema()` | DB |\n"

    )
