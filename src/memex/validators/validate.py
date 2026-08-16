"""Adversarial validation: a DAG of small, orthogonal LLM-judged criteria.

Runs AFTER node creation (the node and its provenance edges exist), so
evidence is the node's own content plus its parents' contents (read from the
parents' content_path files). The family is a dependency-ordered DAG, not a
flat fan-out:

    V1 (grounding) ──> D7 (quote verification over V1's verdicts)
        │
        └──> V2 (re-elaboration quality; consumes V1's verdicts;
               SKIPPED when V1 has fatal failures — the node is draft
               already, the call is saved, re-derive re-runs both)

Failures carry the criterion id prefix and a severity tag: fatal (D6, D7,
V1-UNSUPPORTED — one is enough → draft) vs quality (V2 — draft, annotated
severity=quality, human-promotable). The tag is an informational annotation
for human review: both severities gate to draft (no separate
quality_failed state) and draft nodes are human-promotable via the review
flow. A judge call or verdict-parse failure degrades to pass-with-warning —
it never crashes the derive.

The judge is the agent that produced the derivation (RPC process reuse;
--no-session keeps each judge call a stateless single turn), or the agent
pointed at by ``MEMEX_JUDGE`` when set. ``MEMEX_VALIDATION=off`` disables the
whole DAG; the deterministic checks D1–D6 never opt out (D7 is vacuous
without V1's verdicts).
"""

from __future__ import annotations

import json as _json
import os
import re
import sqlite3
import sys as _sys
from pathlib import Path
from typing import Any, Callable

from memex.agent import Agent, load_agent
from memex.checks import CheckResult
from memex.rules import (
    SEVERITY_FATAL,
    VALIDATION_RULES,
    ValidationRule,
    _WIKILINK_RE,
    _strip_frontmatter,
)
from memex.utils.parsing import (
    _MAX_PROMPT_CHARS,
    _TRUNCATION_NOTE,
    _cap_prompt_content,
    parse_synthesis_statements,
)

# Quote match: literal substring, with a whitespace-collapsed fallback (LLMs
# re-wrap line breaks; a fabricated quote differs in words, not whitespace).
_WS_RE = re.compile(r"\s+")


def _decode_statements(raw: str | None) -> list[str]:
    """Synthesis statements from the DB column (JSON array of strings).

    Shared parse with the D3 deterministic check (``parse_synthesis_statements``):
    a null/empty column, invalid JSON, or a non-list payload means no
    statements, never a crash.
    """
    return parse_synthesis_statements(raw)


def _load_parents(
    con: sqlite3.Connection, node_id: str
) -> list[dict[str, Any]]:
    """Provenance parents of *node_id* with their file contents (when readable)."""
    rows = con.execute(
        """
        SELECT to_node FROM edge
        WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
        """,
        (node_id,),
    ).fetchall()
    parents: list[dict[str, Any]] = []
    for (pid,) in rows:
        row = con.execute(
            """
            SELECT n.content_path, s.title
            FROM node n
            LEFT JOIN source s ON s.node_id = n.id
            WHERE n.id = ?
            """,
            (pid,),
        ).fetchone()
        content_path = row[0] if row is not None else None
        content = None
        if content_path and Path(content_path).exists():
            try:
                content = Path(content_path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # Unreadable (missing/permission) or invalid UTF-8 (latin-1
                # scraped pages, binary blobs placed in the vault): degrade
                # to content=None — the documented 'content unavailable'
                # path — never let UnicodeDecodeError (a ValueError) crash
                # the derive after the node row and file were created.
                content = None
            else:
                # NUL bytes (PDF ToUnicode artifacts) never reach the judge
                # (``_cap_prompt_content`` strips them from the prompt copy);
                # strip them at load so D7's local quote comparison verifies
                # against the same surface the judge actually saw.
                content = content.replace("\x00", "")
        parents.append(
            {
                "node_id": pid,
                "filename": Path(content_path).stem if content_path else pid,
                "content_path": content_path,
                "content": content,
                "title": row[1] if row is not None else None,
            }
        )
    return parents


def _parent_block(
    parents: list[dict[str, Any]],
    allow_read: bool,
    budget: int = _MAX_PROMPT_CHARS,
) -> str:
    """Render the parent evidence block for a validation prompt.

    Reader judges (``allow_read``) get path references and read the files
    themselves; other judges get the contents inlined, keyed by filename —
    the resolution V1's link rule and D7's quote verification rely on.
    ``budget`` (default ``_MAX_PROMPT_CHARS``) bounds the WHOLE block —
    ``_run_wave`` passes the remainder of the total prompt budget so the
    parents plus the template, slices and body fit the judge's window.

    Inlined content is NUL-stripped and size-capped (``_cap_prompt_content``,
    the same guard the derive path applies to source content): extraction
    can produce multi-megabyte parent files that would overflow the judge's
    context window and silently degrade the wave to pass-with-warning. The
    cap is CUMULATIVE across parents: when the inlined contents would
    together exceed the budget, it is allocated across them proportionally
    to their size (headers, separators, the truncation notes of every
    sliced parent and the "(content unavailable)" suffix of every
    unreadable parent reserved up front), so every parent keeps a
    representative slice and a synthesis over several large parents never
    concatenates N near-cap blocks into a prompt that overflows the
    judge's window and silently skips the waves it matters most for. When
    the reservation itself reaches the budget (content budget clamped to
    0), the joined block is clamped as a whole, so an extreme parent count
    can never overflow the judge's window either. Parent content is
    NUL-stripped at load (``_load_parents``), so D7's local quote
    comparison verifies against the same surface the judge saw; only the
    size cap is prompt-side — D7 keeps the full content.
    """
    blocks: list[str] = []
    headers = [
        f"Parent {i}: {parent['filename']} (node {parent['node_id']}"
        + (f", title: {parent['title']}" if parent.get("title") else "")
        + ")"
        for i, parent in enumerate(parents, start=1)
    ]
    # NUL-strip up front: the aggregate budget must measure the surface the
    # judge actually sees (the same stripping _cap_prompt_content applies).
    inlined: list[tuple[int, str]] = [
        (i, parent["content"].replace("\x00", ""))
        for i, parent in enumerate(parents)
        if parent["content"] is not None
    ]
    total = sum(len(c) for _, c in inlined)
    # Headers, separators, the "(content unavailable)" suffix of every
    # unreadable parent, and the truncation notes of every sliced parent
    # are part of the joined block — reserve their space so the capped
    # contents plus framing stay inside the budget. The reservation itself
    # is clamped against the budget: an extreme parent count must not let
    # headers+notes+suffixes alone blow past it.
    framing = (
        sum(len(h) + 1 for h in headers)
        + 2 * max(0, len(parents) - 1)
        + len("\n(content unavailable)") * (len(parents) - len(inlined))
    )
    framing = min(framing, budget)
    limits: dict[int, int] = {}
    if not allow_read and inlined and total > budget - framing:
        content_budget = max(
            0, budget - framing - len(_TRUNCATION_NOTE) * len(inlined)
        )
        # Proportional allocation: each parent keeps a size-weighted slice
        # (floors sum below the budget; the remainder is distributed one
        # char at a time, never exceeding it). Every inlined parent's slice
        # is smaller than its content when the joined content overflows, so
        # each appends the truncation note — reserved above.
        floors = [content_budget * len(c) // total for _, c in inlined]
        remainder = content_budget - sum(floors)
        for k, (idx, _) in enumerate(inlined):
            limits[idx] = floors[k] + (1 if k < remainder else 0)
    for i, parent in enumerate(parents):
        header = headers[i]
        if allow_read:
            blocks.append(
                f"{header}\n  path: {parent['content_path']}\n"
                "  Read this file yourself with the read tool before judging."
            )
        elif parent["content"] is not None:
            blocks.append(
                f"{header}\n"
                f"{_cap_prompt_content(parent['content'], limits.get(i, budget))}"
            )
        else:
            blocks.append(f"{header}\n(content unavailable)")
    joined = "\n\n".join(blocks)
    # Last resort: when the reservation itself reaches the budget
    # (content budget clamped to 0), the headers+notes+suffixes can still
    # exceed it — clamp the whole block so it never overflows the judge's
    # window.
    return _cap_prompt_content(joined, budget)


def _fill_template(template: str, **kwargs: str) -> str:
    """Fill ``{placeholders}`` in ONE pass over the template.

    Sequential replaces are order-dependent: a parent file or the node body
    containing the literal text "{v1_verdicts}" (or "{parents}", "{slices}")
    would be clobbered by a later fill, rewriting the judge's evidence with
    a rendered block. A single regex pass substitutes every ``{word}``
    placeholder directly from the kwargs — interpolated values are inserted
    once and never re-scanned, so placeholder-looking text inside content is
    preserved verbatim. Unknown ``{words}`` and the literal JSON braces in
    the payload examples are left untouched.
    """
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: kwargs.get(m.group(1), m.group(0)),
        template,
    )


def _warn(message: str) -> None:
    _sys.stderr.write(_json.dumps({"validation_warning": message}) + "\n")


def validation_environment(agent: Agent) -> tuple[Agent, bool]:
    """Resolve the validation judge and enabled flag for a service.

    The judge is the agent that produced the derivation by default (RPC
    process reuse; role separation lives at the prompt level), or the agent
    named by ``MEMEX_JUDGE`` when set. The LLM-judged criteria (V1–V2) are
    always-on; ``MEMEX_VALIDATION=off`` disables only them — the
    deterministic checks D1–D6 never opt out.

    Returns (judge, enabled).
    """
    judge_path = os.environ.get("MEMEX_JUDGE")
    judge = load_agent(judge_path) if judge_path else agent
    enabled = os.environ.get("MEMEX_VALIDATION", "").lower() != "off"
    return judge, enabled


def merge_gate_failures(
    check_result: CheckResult, validation_result: CheckResult | None
) -> tuple[list[str], str]:
    """Merge the deterministic and validation gates into one verdict.

    Gate contract: D + V failures accumulate into a single list — V
    failures are appended after D failures (the deterministic checks and D7
    run first; V2 quality annotations ride after them) — and any failure
    at all gates to ``draft``. Returns (failures, trust_state).
    """
    failures = list(check_result.failures)
    if validation_result is not None:
        failures.extend(validation_result.failures)
    trust_state = "auto-verified" if not failures else "draft"
    return failures, trust_state


def _call_judge(
    call: Callable[..., str], judge: Agent, prompt: str, allow_read: bool
) -> tuple[str | None, dict[str, Any] | None]:
    """One judge turn. Returns (raw, payload); (None, None) when the call fails."""
    try:
        try:
            raw = call(prompt, allow_read=allow_read)
        except TypeError:
            # Legacy judge callables without the allow_read keyword.
            raw = call(prompt)
    except Exception:  # noqa: BLE001
        return None, None
    payload = None
    getter = getattr(judge, "last_tool_payload", None)
    if callable(getter):
        payload = getter("submit_verdicts")
    return raw, payload


def _cited_sources(
    claim: str, tier: str | None, parents: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The sources a SUPPORTED verdict for *claim* is checked against.

    Syntheses: the parents named by ANY of the claim's inline links
    (cross-source resolution — a multi-link aggregation claim may be quoted
    from any linked parent). Notes: the (single) parent, unconditionally —
    the D6 notes-tier exemption, so a stray wikilink in note prose never
    changes what the note is grounded against. A synthesis claim with no
    link has no cited source — V1's missing-declaration rule should have
    flagged it; D7 has nothing to verify against.
    """
    if tier != "synthesis":
        return parents
    links = _WIKILINK_RE.findall(claim)
    if not links:
        return []
    filenames = {filename for filename, _ in links}
    return [p for p in parents if p["filename"] in filenames]


def _quote_in_source(quote: str, content: str) -> bool:
    """Literal match, with a whitespace-collapsed fallback.

    Surface-invariant across judge surfaces: an inline judge sees the
    NUL-stripped prompt copy, but a READER judge echoes the RAW parent file
    (which may carry PDF ToUnicode NUL bytes) — strip NUL from the quote so
    both surfaces verify against the same NUL-stripped local content (the
    whitespace-collapse fallback alone cannot remove ``\\x00``).
    """
    quote = quote.replace("\x00", "").strip()
    if not quote:
        return False
    if quote in content:
        return True
    return _WS_RE.sub(" ", quote) in _WS_RE.sub(" ", content)


def _d7_verify_quotes(
    verdicts: list[dict[str, Any]],
    node: dict[str, Any],
    parents: list[dict[str, Any]],
    slices: list[str] | None = None,
) -> list[str]:
    """D7 (deterministic): every evidence_quote V1 cites for a SUPPORTED
    verdict must appear literally in the cited source (linked parent for
    syntheses, single parent for notes). Quote not found → failure.

    The cited source (and the COMMON_KNOWLEDGE link-presence test) is
    resolved from the claim text actually PRESENTED to the judge, not the
    verdict's echoed claim: the V1 contract tells the judge to echo each
    claim verbatim, but LLMs routinely echo/normalize claim text. A judge
    that drops the [[filename|alias]] markers from a properly linked claim
    would otherwise make ``_cited_sources`` return [] and draft an honest
    node whose quote is genuine; a judge that ADDS a link to a link-free
    claim would smuggle it past the missing-declaration rule. Each verdict
    is therefore correlated to its presented slice (the same two-phase
    correlation the coverage guard uses: link-aware keys — [[filename|alias]]
    → filename — so claims differing only in their link target stay distinct
    instances, with the link-stripped fallback for echoes that dropped the
    markers); only a verdict matching no slice falls back to the echoed
    claim text.

    Also backstops COMMON_KNOWLEDGE — the third verdict class has no quote
    and no negative contract: in a synthesis, a COMMON_KNOWLEDGE verdict on
    a claim with no inline link would silently exempt a source-derived fact
    from the missing-declaration rule. The link presence is checkable in
    code, so such a verdict fails deterministically here (the same
    judgement the prompt's rule makes).
    """
    tier = node.get("tier")
    failures: list[str] = []
    _, matched = _correlate_verdicts(slices or [], verdicts)
    for v, match in zip(verdicts, matched):
        echoed = v.get("claim", "")
        claim = _slice_claim_text(slices[match]) if match is not None else echoed
        if v.get("verdict") == "COMMON_KNOWLEDGE":
            if tier == "synthesis" and not _WIKILINK_RE.search(claim):
                failures.append(
                    f"{SEVERITY_FATAL} COMMON_KNOWLEDGE verdict on a link-free "
                    f"synthesis claim is a missing declaration: {claim!r} — a "
                    "source-derived fact without an inline link is UNSUPPORTED"
                )
            continue
        if v.get("verdict") != "SUPPORTED":
            continue
        quote = v.get("evidence_quote", "")
        if not quote.strip():
            failures.append(
                f"{SEVERITY_FATAL} SUPPORTED verdict without an evidence quote "
                f"(claim: {claim!r})"
            )
            continue
        sources = _cited_sources(claim, tier, parents)
        if not sources:
            failures.append(
                f"{SEVERITY_FATAL} Evidence quote {quote!r} has no cited source "
                f"to verify against (claim: {claim!r})"
            )
            continue
        if not any(
            source.get("content") is not None
            and _quote_in_source(quote, source["content"])
            for source in sources
        ):
            names = ", ".join(s["filename"] for s in sources)
            failures.append(
                f"{SEVERITY_FATAL} Evidence quote not found in {names}: {quote!r}"
            )
    return failures


def _render_v1_verdicts(verdicts: list[dict[str, Any]]) -> str:
    """Render V1's per-claim verdicts for V2's grounding block."""
    lines: list[str] = []
    for v in verdicts:
        line = f'- "{v.get("claim", "")}" \u2192 {v.get("verdict", "")}'
        if v.get("evidence_quote"):
            line += f" (evidence: {v['evidence_quote']})"
        if v.get("source_examined"):
            line += f" (source_examined: {v['source_examined']})"
        lines.append(line)
    return "\n".join(lines) if lines else "(no verdicts)"


# Deterministic DAG stages keyed by the wave whose verdicts they verify:
# D7 runs immediately after V1's wave and checks V1's evidence quotes. This
# is the only hardcoded stage — LLM-judged criteria live in VALIDATION_RULES
# with order/depends_on/skip_when_fatal fields (adding a criterion never
# touches run_validations).
_DETERMINISTIC_STAGES: dict[str, Callable[..., list[str]]] = {
    "V1": _d7_verify_quotes,
}


def _slice_claim_text(slice_block: str) -> str:
    """The claim text embedded in a rendered slice block.

    Slices are rendered ``Claim N: "<claim>"`` (V1) or
    ``Statement N: "<claim>"`` (V2) — the leading label and wrapping quotes
    are presentation; the claim itself is what a verdict echoes. The
    closing delimiter is the LAST quote: claims routinely contain embedded
    double quotes (``The author wrote "hello" to the editor.``), and the
    greedy group spans them so the FULL claim text is what verdicts
    correlate against — never a prefix cut at the first quote. V1's
    trailing ``\n  links: …`` resolution line follows the closing quote and
    is left out of the capture. Falls back to the whole block when the
    shape is unexpected.
    """
    m = re.match(r'^(?:Claim|Statement) \d+: "(.*)"(?:\n|$)', slice_block, re.S)
    return m.group(1) if m else slice_block


def _correlate_verdicts(
    slices: list[str], verdicts: list[dict[str, Any]]
) -> tuple[list[str], list[int | None]]:
    """Correlate verdicts to presented slices, one verdict per claim instance.

    Matching is two-phase. Phase 1 keys claim text with inline wikilink
    markers REPLACED BY their target filenames (``[[p-a|A]]`` → ``p-a``):
    two claims that differ only in their link target (``Alpha lives in
    [[p-a|A]].`` vs ``Alpha lives in [[p-b|B]].``) stay distinct instances,
    so verdicts in any order consume the RIGHT claim — never the other's
    instance. Phase 2 falls back to the link-stripped key for verdicts
    phase 1 could not place: echoes that dropped the [[...]] markers
    entirely (the LLM echo/normalize path the judge is told to avoid but
    routinely takes), and echoes that ADDED markers to a link-free claim —
    always across at least one link-free side, so a marker-carrying echo
    never consumes a differently-linked instance. Each verdict
    consumes ONE presented instance (multiset semantics): N identical
    presented claims need N verdicts, and a duplicate verdict past the
    instance count matches nothing.

    Returns (claims, matched): ``claims`` is the normalized key of each
    slice (index-aligned with ``slices``); ``matched[i]`` is the index of
    the slice the i-th verdict consumed, or None when the verdict matched
    no remaining instance (a duplicate verdict, or claim text never
    presented). Verdicts with empty claim text match nothing.
    """

    def _norm(text: str) -> str:
        return _WS_RE.sub(" ", _WIKILINK_RE.sub("", text or "")).strip()

    def _norm_linked(text: str) -> str:
        # [[filename|alias]] → filename: link-target filenames survive the
        # key so claims differing only in their link target stay distinct.
        return _WS_RE.sub(" ", _WIKILINK_RE.sub(r"\1", text or "")).strip()

    claims = [_norm(_slice_claim_text(s)) for s in slices]
    linked_claims = [_norm_linked(_slice_claim_text(s)) for s in slices]

    # Phase 1 — link-aware keys.
    remaining: dict[str, list[int]] = {}
    for i, claim in enumerate(linked_claims):
        remaining.setdefault(claim, []).append(i)
    matched: list[int | None] = []
    consumed: set[int] = set()
    for v in verdicts:
        vc = _norm_linked(v.get("claim", ""))
        if vc and remaining.get(vc):
            idx = remaining[vc].pop(0)
            matched.append(idx)
            consumed.add(idx)
        else:
            matched.append(None)

    # Phase 2 — link-stripped fallback for verdicts phase 1 could not place:
    # echoes that DROPPED the [[...]] markers, and echoes that ADDED markers
    # to a link-free claim (both LLM echo/normalize failure modes). A
    # fallback match is only made across at least one link-free side: an
    # echo that still carries its own markers must never consume an
    # instance whose linked key names a DIFFERENT target (a duplicate
    # verdict must not steal another claim's instance). Instances already
    # consumed in phase 1 are excluded so nothing is double-consumed.
    remaining = {}
    for i, claim in enumerate(claims):
        if i not in consumed:
            remaining.setdefault(claim, []).append(i)
    for i, v in enumerate(verdicts):
        if matched[i] is not None:
            continue
        echo = v.get("claim", "")
        vc = _norm(echo)
        if not vc or not remaining.get(vc):
            continue
        echo_link_free = _norm_linked(echo) == vc
        for j in list(remaining[vc]):
            if echo_link_free or linked_claims[j] == claims[j]:
                remaining[vc].remove(j)
                matched[i] = j
                break
    return claims, matched


def _verdict_coverage_warnings(
    rule_id: str, slices: list[str], verdicts: list[dict[str, Any]]
) -> list[str]:
    """Coverage gaps when verdicts are correlated to the presented claims.

    A bare count comparison (fewer verdicts than slices) never checks WHICH
    claims were judged: a judge returning N verdicts for the same claim, or
    verdicts whose claim text was never presented (LLMs routinely
    echo/normalize claim text), satisfies the count while the presented
    claims sail through unjudged. Each verdict is therefore matched to a
    slice by the same two-phase correlation as D7 (link-aware keys first —
    [[filename|alias]] → filename — then the link-stripped fallback for
    echoes that dropped the markers), one verdict per presented claim
    INSTANCE: when two slices normalize to the same text — a duplicated
    sentence, claims differing only in whitespace — a single verdict
    covers only one of them. Every slice whose instance count is never
    consumed warns as an unjudged claim, and stray verdicts (duplicates or
    claim text never presented) warn as a set-level gap.
    """
    claims, matched = _correlate_verdicts(slices, verdicts)
    judged_slices = {j for j in matched if j is not None}
    stray = sum(1 for m in matched if m is None)
    warnings: list[str] = []
    unjudged = [
        (block, claim)
        for block, claim, idx in zip(slices, claims, range(len(slices)))
        if idx not in judged_slices
    ]
    if unjudged:
        warnings.append(
            f"{rule_id} verdict shortfall: {len(unjudged)} of {len(slices)} "
            "presented claims were not judged; grounding coverage is incomplete"
        )
        for block, claim in unjudged:
            label = claim if claim else block
            warnings.append(
                f"{rule_id} verdict coverage gap: claim {label!r} was not "
                "judged (no verdict matched the presented claim text)"
            )
    if stray:
        warnings.append(
            f"{rule_id} verdict coverage gap: {stray} verdict(s) were stray "
            "(duplicate claims or claim text never presented); one verdict "
            "per presented claim expected"
        )
    return warnings


def _run_wave(
    rule: ValidationRule,
    call: Callable[..., str],
    judge: Agent,
    content: str,
    node: dict[str, Any],
    parents: list[dict[str, Any]],
    context: str,
    allow_read: bool,
    **extra: str,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Run one LLM-judged wave: slice → prompt → judge turn → parse.

    Returns (failures, verdicts, slices). A judge-call or verdict-parse
    failure degrades to a warning and an empty verdict set — it never
    raises. When the rule expects one verdict per slice, every presented
    claim without a matching verdict (whitespace-normalized claim text,
    link markers ignored — duplicates and echoed claim text included) also
    warns: an incomplete grounding pass must never be silently clean. The
    slices are returned so the deterministic stage for the wave (D7) can
    resolve verdicts against the claim text actually presented.
    """
    try:
        slices = rule.slicer(content, node, parents)
    except Exception as exc:  # noqa: BLE001
        _warn(f"{rule.id} evidence slicing failed, validation skipped: {exc}")
        return [], [], []
    if not slices:
        # Zero claims from a non-empty body (e.g. a body made entirely of
        # list items/blockquotes/tables stripped by _unadorned_prose): the
        # wave is skipped — warn, never silently (the one incomplete
        # coverage case with no verdict set to count).
        if content.strip():
            _warn(
                f"{rule.id} no claims to judge: the node body is non-empty "
                "but the slicer produced zero claims; grounding coverage "
                "is incomplete"
            )
        return [], [], []
    body = _cap_prompt_content(_strip_frontmatter(content))
    slices_block = _cap_prompt_content("\n".join(slices))
    # The whole judge prompt — template, context, slices, body, per-rule
    # extras AND the parent block — must fit the judge's context window.
    # Only the parents were budgeted; the node side was unbounded: a
    # D4-legal synthesis body near the 150k ceiling plus a 120k parent
    # block overflows a 200k window, the judge call raises, _call_judge
    # returns (None, None), and the wave silently degrades to
    # pass-with-warning with V1/V2 never having run. Budget the parent
    # block against the remainder (measured with an empty parent block),
    # then clamp the total so the prompt can never exceed _MAX_PROMPT_CHARS
    # even when the body and slices alone saturate it.
    parents_budget = max(
        0,
        _MAX_PROMPT_CHARS
        - len(
            _fill_template(
                rule.prompt_template,
                context=context,
                body=body,
                slices=slices_block,
                parents="",
                **extra,
            )
        ),
    )
    prompt = _fill_template(
        rule.prompt_template,
        context=context,
        body=body,
        slices=slices_block,
        parents=_parent_block(parents, allow_read, budget=parents_budget),
        **extra,
    )
    prompt = _cap_prompt_content(prompt, _MAX_PROMPT_CHARS)
    raw, payload = _call_judge(call, judge, prompt, allow_read)
    if raw is None:
        _warn(f"{rule.id} judge call failed, validation skipped")
        return [], [], []
    try:
        rule_failures, warning, verdicts = rule.verdict_parser(raw, payload)
    except Exception as exc:  # noqa: BLE001
        _warn(f"{rule.id} verdict parse failed, validation skipped: {exc}")
        return [], [], []
    if warning:
        _warn(warning)
    if rule.expects_full_verdicts:
        for coverage_warning in _verdict_coverage_warnings(
            rule.id, slices, verdicts
        ):
            _warn(coverage_warning)
    return rule_failures, verdicts, slices


def run_validations(
    judge: Agent,
    con: sqlite3.Connection,
    node_id: str,
    content_path: Path | str,
) -> CheckResult:
    """Run the validation DAG (V1 → D7 → V2) on a created node.

    Evidence: the node's content file plus its parents' content files
    (parents via ``derived_from`` edges). Failures are prefixed with the
    criterion id ("V1: ...", "D7: ...", "V2: ...") and carry a severity tag.
    The DAG is declarative: ``VALIDATION_RULES`` carries each wave's
    ``order`` / ``depends_on`` / ``skip_when_fatal`` / ``expects_full_verdicts``
    fields; D7 is the deterministic stage keyed to V1's wave. V2 is skipped
    when V1 produces fatal failures. A judge call or verdict-parse failure
    produces a warning and skips that wave — it never raises; a V1 verdict
    shortfall, or a non-empty body that yields zero claims, also warns
    (never a silent clean pass with partial coverage). A judge without a
    call_llm seam (e.g. DemoAgent) warns and skips the whole family —
    the always-on quality gate is never silently disabled.

    Args:
        judge:        The validation judge (Agent seam; must expose call_llm).
        con:          Open SQLite connection.
        node_id:      The derivation node id to validate.
        content_path: Path to the derivation's markdown file.

    Returns:
        CheckResult with .passed=True and .failures=[] if all rules pass,
        or .passed=False and .failures carrying per-criterion messages.
    """
    content_path = Path(content_path)
    try:
        content = content_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Same degradation as an unreadable file: invalid UTF-8 bytes in the
        # node's own markdown must not crash the derive with a traceback —
        # the node is drafted with a fatal, evidence-cannot-be-read failure.
        return CheckResult(
            passed=False,
            failures=[f"{SEVERITY_FATAL} Validation content read failed: {exc}"],
        )

    node_row = con.execute(
        "SELECT tier, kind, synthesis_statements FROM node WHERE id = ?",
        (node_id,),
    ).fetchone()
    node: dict[str, Any] = {
        "tier": node_row[0] if node_row is not None else None,
        "kind": node_row[1] if node_row is not None else None,
        "synthesis_statements": (
            _decode_statements(node_row[2]) if node_row is not None else []
        ),
    }

    parents = _load_parents(con, node_id)
    if not parents:
        # Nothing to ground against; the deterministic D1 gate already flags
        # a parentless node — validation has no evidence to judge.
        return CheckResult(passed=True, failures=[])

    call = getattr(judge, "call_llm", None)
    if not callable(call):
        # Judge without a call_llm seam (e.g. DemoAgent): the V1/V2 family
        # cannot run — warn, never skip silently. The deterministic checks
        # D1–D6 remain the gate, but the advertised always-on quality gate
        # must never be disabled without a signal.
        _warn(
            "V1/V2 validation skipped: judge "
            f"{type(judge).__name__} has no call_llm seam; the LLM-judged "
            "quality gate did not run"
        )
        return CheckResult(passed=True, failures=[])

    allow_read = bool(getattr(judge, "can_read_files", False) and parents)
    tier = node["tier"] or "unknown"
    context = (
        f"Node tier: {tier}. The node was just created from the parents listed "
        "below; the validation DAG runs after the deterministic checks."
    )

    failures: list[str] = []
    verdicts_by_rule: dict[str, list[dict[str, Any]]] = {}

    # Waves execute in ascending order; each rule declares its dependencies
    # and skip condition in the registry. D7 (deterministic) verifies V1's
    # quotes inside V1's wave via _DETERMINISTIC_STAGES; V2 declares
    # depends_on=("V1",) + skip_when_fatal, so it always runs after D7.
    for rule in sorted(VALIDATION_RULES, key=lambda r: r.order):
        missing = [d for d in rule.depends_on if d not in verdicts_by_rule]
        if missing:
            _warn(
                f"{rule.id} skipped: dependencies {', '.join(missing)} "
                "did not run"
            )
            continue
        if rule.skip_when_fatal and any(
            SEVERITY_FATAL in f and f.startswith(f"{dep}: ")
            for dep in rule.depends_on
            for f in failures
        ):
            _warn(
                f"{rule.id} skipped: {'/'.join(rule.depends_on)} produced "
                "fatal failures"
            )
            continue
        rule_failures, verdicts, slices = _run_wave(
            rule, call, judge, content, node, parents, context, allow_read,
            v1_verdicts=_render_v1_verdicts(verdicts_by_rule.get("V1", [])),
        )
        verdicts_by_rule[rule.id] = verdicts
        failures.extend(f"{rule.id}: {f}" for f in rule_failures)
        stage = _DETERMINISTIC_STAGES.get(rule.id)
        if stage is not None:
            try:
                d7_failures = stage(verdicts, node, parents, slices=slices)
            except Exception as exc:  # noqa: BLE001
                _warn(f"D7 verification failed, skipped: {exc}")
                d7_failures = []
            failures.extend(f"D7: {f}" for f in d7_failures)

    return CheckResult(passed=len(failures) == 0, failures=failures)
