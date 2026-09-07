"""Adversarial validation: a DAG of small, orthogonal LLM-judged criteria.

Runs AFTER node creation (the node and its provenance edges exist), so
evidence is the node's own content plus its parents' contents (read from the
parents' content_path files). The family is a dependency-ordered DAG, not a
flat fan-out:

    V1 (grounding) ──> D7 (deterministic grounding resolution over V1's
        verdicts: the system locates the evidence span in the cited parent
        and token-gates the claim)
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

import os
import re
import sqlite3
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
from memex.validators.evidence import _warn, grounding_gate, resolve_evidence


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
                # strip them at load so D7's local evidence resolution runs
                # against the same surface the judge actually saw.
                content = content.replace("\x00", "")
        parents.append(
            {
                "node_id": pid,
                "key": f"P{len(parents) + 1}",
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
    the resolution V1's link rule and D7's grounding resolution rely on.
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
    NUL-stripped at load (``_load_parents``), so D7's local evidence
    resolution runs against the same surface the judge saw; only the
    size cap is prompt-side — D7 keeps the full content.
    """
    blocks: list[str] = []
    headers = [
        f"Parent {parent['key']}: {parent['filename']} (node {parent['node_id']}"
        + (f", title: {parent['title']}" if parent.get("title") else "")
        + ")"
        for parent in parents
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


def _d7_resolve_grounding(
    verdicts: list[dict[str, Any]],
    node: dict[str, Any],
    parents: list[dict[str, Any]],
    slices: list[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """D7 (deterministic): resolve every SUPPORTED verdict's evidence.

    The judge emits only references plus an optional ``evidence_hint``
    locator; the system LOCATES the supporting span in the cited parent
    (``resolve_evidence``: sliding-window token coverage over the
    normalized source) and the grounding gate decides whether the CLAIM's
    content tokens are covered by that span. Not grounded → D7 fatal (the
    system overrides the judge); grounded → one evidence record
    ``{claim_index, parent_key, span_text, confidence, resolver}`` per
    claim (I5). UNSUPPORTED verdicts are never resolved and never produce
    a record (I3); COMMON_KNOWLEDGE on a synthesis claim whose slice
    carries no inline link is backstopped as a missing declaration.

    The parent is resolved from the judge's parent_key reference (P1..Pn)
    — a missing/invalid key falls back to any readable parent (today's
    bad-key rule) — and the claim text is recovered from the slice by
    claim_index. A SUPPORTED verdict whose claim_index does not correlate
    to a presented slice has no claim text to gate → D7 fatal, no record.
    Unreadable parent content → cannot resolve → D7 fatal (same draft
    outcome as the old anchor-not-found path).
    """
    tier = node.get("tier")
    key_to_parent = {p["key"]: p for p in parents}
    failures: list[str] = []
    records: list[dict[str, Any]] = []
    resolved: set[int] = set()
    matched = _correlate_verdicts(slices or [], verdicts)
    for v, match in zip(verdicts, matched):
        idx = v.get("claim_index")
        if match is None or not slices:
            claim = None
        else:
            claim = _slice_claim_text(slices[match])
        if v.get("verdict") == "COMMON_KNOWLEDGE":
            claim_text = claim if claim is not None else f"#{idx}"
            if tier == "synthesis" and not _WIKILINK_RE.search(claim_text):
                failures.append(
                    f"{SEVERITY_FATAL} COMMON_KNOWLEDGE verdict on a link-free "
                    f"synthesis claim is a missing declaration: {claim_text!r} — a "
                    "source-derived fact without an inline link is UNSUPPORTED"
                )
            continue
        if v.get("verdict") != "SUPPORTED":
            continue
        if isinstance(idx, int) and idx in resolved:
            # Duplicate SUPPORTED verdict for an already-handled
            # claim_index (the V1 coverage machinery already counts
            # duplicate references as a coverage gap): I5 wants exactly one
            # record per grounded claim — only the first verdict resolves.
            continue
        if isinstance(idx, int):
            resolved.add(idx)
        hint = v.get("evidence_hint")
        hint_text = hint if isinstance(hint, str) else ""
        if claim is None:
            # No presented slice for this claim_index: there is no claim
            # text to token-gate against — cannot resolve, must not crash.
            failures.append(
                f"{SEVERITY_FATAL} SUPPORTED verdict claims #{idx}, which does "
                "not correlate to any presented claim; no claim text to ground"
            )
            continue
        # candidate = evidence_hint or the claim itself (PRD cascade).
        candidate = hint_text.strip() or claim
        if tier == "synthesis":
            pk = v.get("parent_key", "")
            sources = [key_to_parent[pk]] if pk in key_to_parent else []
            if not sources:
                # parent_key missing/invalid: fall back to any readable
                # parent, so a genuinely grounded claim is never drafted on
                # a bad key.
                sources = list(parents)
        else:
            # Notes tier: judged against the single parent regardless of
            # any inline links the claim carries.
            sources = list(parents)
        grounded: dict[str, Any] | None = None
        for source in sources:
            content = source.get("content")
            if content is None:
                continue
            evidence = resolve_evidence(candidate, content)
            if evidence is not None and grounding_gate(claim, evidence):
                grounded = {
                    "claim_index": idx,
                    "parent_key": source["key"],
                    "span_text": evidence.span_text,
                    "confidence": evidence.confidence,
                    "resolver": evidence.resolver,
                }
                break
        if grounded is not None:
            records.append(grounded)
            continue
        names = ", ".join(s["key"] for s in sources) or "(no parents)"
        message = (
            f"{SEVERITY_FATAL} SUPPORTED claim not grounded in the cited "
            f"source content (claim: {claim!r}; parents examined: {names})"
        )
        if hint_text:
            message += f" [evidence_hint: {hint_text!r}]"
        failures.append(message)
    return failures, records


def _render_v1_verdicts(verdicts: list[dict[str, Any]]) -> str:
    """Render V1's per-claim verdicts for V2's grounding block."""
    lines: list[str] = []
    for v in verdicts:
        line = f'- #{v.get("claim_index", "?")} \u2192 {v.get("verdict", "")}'
        if v.get("parent_key"):
            line += f" (parent: {v['parent_key']})"
        if v.get("evidence_hint"):
            line += f" (hint: {v['evidence_hint']})"
        if v.get("absence_explanation"):
            line += f" (absence: {v['absence_explanation']})"
        lines.append(line)
    return "\n".join(lines) if lines else "(no verdicts)"


# Deterministic DAG stages keyed by the wave whose verdicts they resolve:
# D7 runs immediately after V1's wave and resolves V1's grounding over its
# verdicts (failures + evidence records). This is the only hardcoded stage
# — LLM-judged criteria live in VALIDATION_RULES with
# order/depends_on/skip_when_fatal fields (adding a criterion never
# touches run_validations).
_DETERMINISTIC_STAGES: dict[str, Callable[..., tuple[list[str], list[dict[str, Any]]]]] = {
    "V1": _d7_resolve_grounding,
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
) -> list[int | None]:
    """Map each verdict to its slice index by claim_index (1-based).

    The judge references claims by their presented index ("Claim N:"), so
    correlation is a pure index lookup — no text matching, no echo
    normalization. ``matched[i]`` is the slice index the i-th verdict
    consumed, or None when its claim_index is out of range (a stray or
    duplicate verdict).
    """
    matched: list[int | None] = []
    for v in verdicts:
        idx = v.get("claim_index")
        if isinstance(idx, int) and 1 <= idx <= len(slices):
            matched.append(idx - 1)
        else:
            matched.append(None)
    return matched


def _verdict_coverage_warnings(
    rule_id: str, slices: list[str], verdicts: list[dict[str, Any]]
) -> list[str]:
    """Coverage gaps when verdicts are correlated to the presented claims.

    Verdicts reference claims by index, so coverage is a set comparison of
    referenced indices against the presented slice range. Every slice whose
    index is never referenced warns as an unjudged claim, and verdicts
    whose claim_index is out of range (stray or duplicate) warn as a
    set-level gap.
    """
    matched = _correlate_verdicts(slices, verdicts)
    judged_slices = {j for j in matched if j is not None}
    stray = sum(1 for m in matched if m is None)
    warnings: list[str] = []
    unjudged = [
        (i, _slice_claim_text(s))
        for i, s in enumerate(slices)
        if i not in judged_slices
    ]
    if unjudged:
        warnings.append(
            f"{rule_id} verdict shortfall: {len(unjudged)} of {len(slices)} "
            "presented claims were not judged; grounding coverage is incomplete"
        )
        for idx, claim in unjudged:
            warnings.append(
                f"{rule_id} verdict coverage gap: claim {claim!r} was not "
                "judged (no verdict referenced its index)"
            )
    if stray:
        warnings.append(
            f"{rule_id} verdict coverage gap: {stray} verdict(s) were stray "
            "(duplicate or out-of-range claim_index); one verdict per "
            "presented claim expected"
        )
    return warnings


def _enrich_claim_text(failures: list[str], slices: list[str]) -> list[str]:
    """Splice the presented claim text into index-referenced failures.

    The V1 parser emits "Unsupported claim #N" (it has no slice access);
    readability is restored here by inserting the claim text after the
    index so a draft failure names the claim it flags.
    """
    enriched: list[str] = []
    for f in failures:
        m = re.search(r"Unsupported claim #(\d+)\b", f)
        if m and 1 <= int(m.group(1)) <= len(slices):
            text = _slice_claim_text(slices[int(m.group(1)) - 1])
            pos = m.end()
            enriched.append(f[:pos] + f": {text}" + f[pos:])
        else:
            enriched.append(f)
    return enriched


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
    rule_failures = _enrich_claim_text(rule_failures, slices)
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
        .evidence carries one grounded-evidence record per grounded
        SUPPORTED claim (empty when nothing grounded).
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
    evidence_records: list[dict[str, Any]] = []
    verdicts_by_rule: dict[str, list[dict[str, Any]]] = {}

    # Waves execute in ascending order; each rule declares its dependencies
    # and skip condition in the registry. D7 (deterministic) resolves V1's
    # grounding inside V1's wave via _DETERMINISTIC_STAGES; V2 declares
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
                d7_failures, d7_records = stage(
                    verdicts, node, parents, slices=slices
                )
            except Exception as exc:  # noqa: BLE001
                _warn(f"D7 resolution failed, skipped: {exc}")
                d7_failures, d7_records = [], []
            failures.extend(f"D7: {f}" for f in d7_failures)
            evidence_records.extend(d7_records)

    return CheckResult(
        passed=len(failures) == 0, failures=failures, evidence=evidence_records
    )
