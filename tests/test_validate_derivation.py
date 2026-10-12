"""Tests for run_validations() — the validation DAG (V1 → D7 → V2).

Covers the dispatch paths of the always-on adversarial validations:
  - all-SUPPORTED judge → CheckResult(passed=True, failures=[]) + one
    grounded-evidence record per SUPPORTED claim (I5)
  - UNSUPPORTED claim → fatal failure prefixed with the rule id ("V1: ...")
    carrying parent_key + absence_explanation (negative-verdict contract)
  - synthesis claim without an inline link → UNSUPPORTED (missing declaration)
  - claim linked to a parent that does not support it → failure
  - D7: SUPPORTED verdict whose claim is not grounded in the cited parent
    (fabricated hint or claim absent from the parent) → draft (I1, I6)
  - D7: a SUPPORTED verdict without an evidence_hint still grounds when the
    claim itself is token-covered by the parent (missing hint alone is no
    longer a failure)
  - D7: a claim_index that does not correlate to a presented slice → fatal,
    no record (must not crash)
  - D7: parent_key missing/invalid → fall back to any readable parent
  - evidence records: UNSUPPORTED/COMMON_KNOWLEDGE verdicts never produce a
    record (I3); grounded spans are verbatim substrings of the normalized
    parent (I4)
  - DAG: when V1 produces fatal failures, V2 is skipped (no judge call)
  - V2 boilerplate → quality-level failure ("V2: ...", severity=quality)
  - judge raising → pass-with-warning (graceful degradation, never raises)
  - judge returning non-JSON → pass-with-warning
  - judge without call_llm (DemoAgent) → pass-with-warning (never silently skipped)
  - reader judges receive allow_read=True and path references
  - fixture groundings: under the D7 token-coverage gate a claim must
    actually be covered by the cited parent's content (ticket #152 scope 9),
    so every fixture's claim sentences are drawn verbatim from its parents.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memex.derivers.demo import DemoAgent
from memex.validators.validate import run_validations
from tests.fake_llm_client import FakeJudge


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Default single parent content — a sentence pool every generic fixture
# claim is drawn from, so D7's token-coverage gate has real evidence to
# find (the old "This claim is fine."-against-boilerplate fixtures no
# longer ground: a claim must be token-covered by its cited parent).
_PARENT_SENTENCE_1 = (
    "The memex system derives notes from source documents and records "
    "provenance edges."
)
_PARENT_SENTENCE_2 = "This claim is fine and grounded in the cited parent."
_PARENT_SENTENCE_3 = "The source material covers the subject thoroughly."
_DEFAULT_PARENT = (
    f"{_PARENT_SENTENCE_1} {_PARENT_SENTENCE_2} {_PARENT_SENTENCE_3} "
)
_PAD = _PARENT_SENTENCE_3 + " "


def _setup(
    tmp_path: Path,
    *,
    tier: str = "notes",
    prose: str,
    statements: list[str],
    parents: dict[str, str] | None = None,
) -> tuple[sqlite3.Connection, str, Path]:
    """Minimal db: one or more parent nodes + a derivation node + edges.

    ``parents`` maps parent filename -> parent file content. Returns
    (con, node_id, node_path).
    """
    from memex.store import Store

    parents = parents or {"parent.md": _DEFAULT_PARENT}
    db_path = tmp_path / "memex.db"
    with Store.open(db_path) as store:
        store.init_schema()
        node_id = str(uuid.uuid4())
        node_path = tmp_path / "node.md"
        node_path.write_text(prose, encoding="utf-8")
        store.create_node(
            node_id=node_id, kind="summary", tier=tier, depth=2,
            content_path=str(node_path), created_at=_utcnow(),
            synthesis_statements=statements,
        )
        for filename, content in parents.items():
            parent_id = str(uuid.uuid4())
            parent_path = tmp_path / filename
            parent_path.write_text(content, encoding="utf-8")
            store.create_node(
                node_id=parent_id, kind="summary", tier="notes", depth=1,
                content_path=str(parent_path), created_at=_utcnow(),
            )
            store.create_edge(
                edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
                from_node=node_id, to_node=parent_id,
            )
    con = sqlite3.connect(db_path)
    return con, node_id, node_path


# ── Deterministic fake judges ───────────────────────────────────────

class _PromptJudge(FakeJudge):
    """Prompt-parsing fake judge: the V1/V2 verdict generation for the
    always-on validations. The V1 prompt-parsing helpers (_parse_parents,
    _presented_slices) are inherited from FakeJudge
    (tests/fake_llm_client.py) so the two fakes never drift apart."""

    def _v2(self, prompt: str) -> str:
        return json.dumps({"passes": True, "reason": "ok"})


def _supported(
    claim_index: int, parent_key: str | None = None, hint: str | None = None
) -> dict:
    """A SUPPORTED verdict: parent_key reference plus an OPTIONAL
    evidence_hint locator (the parser normalizes a missing hint to '' and
    D7 falls back to the claim text)."""
    verdict = {"claim_index": claim_index, "verdict": "SUPPORTED"}
    if parent_key:
        verdict["parent_key"] = parent_key
    if hint:
        verdict["evidence_hint"] = hint
    return verdict


class _SupportedJudge(_PromptJudge):
    """Every claim SUPPORTED (no hint — the claim text is the candidate);
    V2 passes."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        verdicts = [
            _supported(s["index"], s["key"]) for s in slices
        ]
        return json.dumps({"verdicts": verdicts})


class _UnsupportedJudge(_SupportedJudge):
    """Claims containing SENTINEL → UNSUPPORTED (negative contract)."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        verdicts = []
        for s in slices:
            if "SENTINEL" in s["claim"]:
                verdicts.append(
                    {
                        "claim_index": s["index"],
                        "verdict": "UNSUPPORTED",
                        "parent_key": s["key"] or "parent",
                        "absence_explanation": "source content does not contain the claim",
                    }
                )
            else:
                verdicts.append(_supported(s["index"], s["key"]))
        return json.dumps({"verdicts": verdicts})


class _MissingLinkJudge(_SupportedJudge):
    """Mirrors the V1 prompt contract: in syntheses, a claim without an inline
    link is UNSUPPORTED (missing declaration)."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        is_synthesis = "Node tier: synthesis" in prompt
        verdicts = []
        for s in slices:
            missing = is_synthesis and s["key"] is None
            if missing:
                verdicts.append(
                    {
                        "claim_index": s["index"],
                        "verdict": "UNSUPPORTED",
                        "parent_key": "no linked parent",
                        "absence_explanation": "missing declaration",
                    }
                )
            else:
                verdicts.append(_supported(s["index"], s["key"]))
        return json.dumps({"verdicts": verdicts})


class _LinkedParentJudge(_SupportedJudge):
    """Support is judged against the LINKED parent only (cross-source check).

    A claim carrying a link [[filename|alias]] is UNSUPPORTED unless the
    linked parent's content contains every TOKEN-<ID> marker the claim
    mentions.
    """

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        parents = self._parse_parents(prompt)
        slices = FakeJudge._presented_slices(prompt)
        is_synthesis = "Node tier: synthesis" in prompt
        verdicts = []
        for s in slices:
            if is_synthesis and s["key"] is None:
                verdicts.append(
                    {
                        "claim_index": s["index"],
                        "verdict": "UNSUPPORTED",
                        "parent_key": "no linked parent",
                        "absence_explanation": "missing declaration",
                    }
                )
                continue
            if s["key"]:
                content = parents.get(s["key"], "")
                tokens = re.findall(r"TOKEN-([A-Z0-9]+)", s["claim"])
                if tokens and not all(t in content for t in tokens):
                    verdicts.append(
                        {
                            "claim_index": s["index"],
                            "verdict": "UNSUPPORTED",
                            "parent_key": s["key"],
                            "absence_explanation": "linked parent content does not support the claim",
                        }
                    )
                    continue
            verdicts.append(_supported(s["index"], s["key"]))
        return json.dumps({"verdicts": verdicts})


class _FabricatedHintJudge(_SupportedJudge):
    """SUPPORTED verdicts carry a fabricated evidence_hint that is NOT in
    the cited source — the deterministic resolver cannot ground it."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        verdicts = [
            _supported(
                s["index"],
                s["key"],
                hint="this fabricated quote appears nowhere in the source",
            )
            for s in slices
        ]
        return json.dumps({"verdicts": verdicts})


class _CountingJudge(_UnsupportedJudge):
    """Counts judge calls and V1 verdicts vs presented claims.

    ``calls`` asserts the DAG edges (V2-skip on fatal, V2 runs when clean);
    ``v1_claims`` / ``v1_verdicts`` assert the judge actually judged every
    presented claim — a regression that silently skips the judge turn (an
    early return, a slicer yielding no slices, a judge raising) would leave
    the verdict count short of the claim count instead of passing clean.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.v1_claims = 0
        self.v1_verdicts = 0

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        self.calls += 1
        raw = super().call_llm(prompt, allow_read=allow_read)
        if "Synthesis statements:" not in prompt:
            self.v1_claims = len(FakeJudge._presented_slices(prompt))
            try:
                self.v1_verdicts = len(json.loads(raw)["verdicts"])
            except (json.JSONDecodeError, KeyError, TypeError):
                self.v1_verdicts = 0
        return raw


class _BoilerplateJudge(_SupportedJudge):
    """V2: passes=false when a statement contains SENTINEL-BOILERPLATE."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            section = prompt.split("Synthesis statements:", 1)[1].split("Node body:", 1)[0]
            passes = "SENTINEL-BOILERPLATE" not in section
            return json.dumps(
                {"passes": passes, "reason": "" if passes else "statement is boilerplate"}
            )
        return super().call_llm(prompt, allow_read=allow_read)


class _RaisingJudge(_SupportedJudge):
    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        raise RuntimeError("judge unavailable")


class _GarbageJudge(_SupportedJudge):
    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        return "not json at all"


class _ReaderJudge(_SupportedJudge):
    """Reader judge: records allow_read and whether paths reached the prompt,
    then reads the parent file itself (as a reader agent would)."""

    can_read_files = True

    def __init__(self) -> None:
        self.captured: dict = {}

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        self.captured["allow_read"] = allow_read
        self.captured["has_path"] = "parent.md" in prompt and "path:" in prompt
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        verdicts = [_supported(s["index"], s["key"]) for s in slices]
        return json.dumps({"verdicts": verdicts})


class _PromptCapturingJudge(_PromptJudge):
    """Records the V1 prompt; SUPPORTED verdicts carry no hint (the notes
    exemption — inline links never redirect the source)."""

    def __init__(self) -> None:
        self.v1_prompt: str | None = None

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        if self.v1_prompt is None:
            self.v1_prompt = prompt
        slices = FakeJudge._presented_slices(prompt)
        verdicts = [_supported(s["index"], s["key"]) for s in slices]
        return json.dumps({"verdicts": verdicts})


class _TailMarkerJudge(_PromptJudge):
    """SUPPORTED verdicts with no hint; the judge captures the V1 prompt to
    prove the resolver runs against the FULL parent content while the
    inlined prompt copy is capped."""

    def __init__(self) -> None:
        self.v1_prompt: str | None = None

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        if self.v1_prompt is None:
            self.v1_prompt = prompt
        slices = FakeJudge._presented_slices(prompt)
        verdicts = [_supported(s["index"], s["key"]) for s in slices]
        return json.dumps({"verdicts": verdicts})


class _CountingSupportedJudge(_SupportedJudge):
    """SUPPORTED verdicts (like _SupportedJudge) plus a judge-call counter
    and the captured V1/V2 prompts — proves an oversized prompt
    (multi-parent or node-body side) never silently skips the V1/V2 waves."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.v1_prompt: str | None = None
        self.v2_prompt: str | None = None

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        self.calls += 1
        if "Synthesis statements:" in prompt:
            if self.v2_prompt is None:
                self.v2_prompt = prompt
            return self._v2(prompt)
        if self.v1_prompt is None:
            self.v1_prompt = prompt
        return super().call_llm(prompt, allow_read=allow_read)


class _NulEchoingReaderJudge(_PromptJudge):
    """Reader judge (can_read_files=True) that echoes the RAW parent file:
    its evidence_hint may carry the PDF ToUnicode NUL bytes a raw read
    exposes, which the NUL-stripped prompt surface never shows."""

    can_read_files = True

    def __init__(self) -> None:
        super().__init__()
        self.reader_surface = False

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        self.reader_surface = allow_read
        slices = FakeJudge._presented_slices(prompt)
        verdicts = [
            _supported(
                s["index"],
                s["key"],
                # A locator echoed from the RAW read: the NUL byte sits
                # between the words exactly as reading the un-stripped file
                # shows; normalization strips it before resolution.
                hint="NUL \x00 parent content supporting the claim text. ",
            )
            for s in slices
        ]
        return json.dumps({"verdicts": verdicts})


class _CommonKnowledgeJudge(_SupportedJudge):
    """Every claim COMMON_KNOWLEDGE — the 'needs no source' verdict path."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        return json.dumps(
            {
                "verdicts": [
                    {"claim_index": s["index"], "verdict": "COMMON_KNOWLEDGE"}
                    for s in slices
                ]
            }
        )


class _DuplicateVerdictJudge(_SupportedJudge):
    """Two SUPPORTED verdicts for the SAME claim_index (both groundable).

    Real judges can repeat a reference; the V1 coverage machinery counts
    duplicate verdicts as a coverage gap. D7 must record exactly ONE
    evidence record per grounded claim (I5) — never N for N duplicate
    verdicts.
    """

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        verdict = _supported(slices[0]["index"], slices[0]["key"])
        return json.dumps({"verdicts": [verdict, dict(verdict)]})


# ── Tests ───────────────────────────────────────────────────────────

class TestRunValidations:
    def test_all_supported_passes(self, tmp_path):
        """SUPPORTED verdicts on grounded claims → V1 + D7 + V2 all pass.

        Uses a counting judge: the pass must come from an actual judge turn
        returning one verdict per presented claim — not from an empty
        verdict set degrading to pass-with-warning (which a regression that
        silently skips the judge call would also produce)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                f"# Note\n\n{_PARENT_SENTENCE_2} {_PARENT_SENTENCE_3}.\n\n"
                f"> Synthesis: inf\n\n{_PAD}"
            ),
            statements=["inf"],
        )
        judge = _CountingJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is True
        assert result.failures == []
        # The judge was called (V1 + V2) and returned exactly one verdict
        # per presented claim — the pass is grounded, not degraded.
        assert judge.calls == 2, f"Expected V1 + V2 judge calls, got {judge.calls}"
        assert judge.v1_claims >= 1
        assert judge.v1_verdicts == judge.v1_claims, (
            f"Judge returned {judge.v1_verdicts} verdicts for "
            f"{judge.v1_claims} presented claims"
        )

    def test_unsupported_claim_fails_with_rule_prefix(self, tmp_path):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\nThis claim states the SENTINEL figure.\n\n> Synthesis: inf\n\n{_PAD}",
            statements=["inf"],
        )
        result = run_validations(_UnsupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert any(f.startswith("V1:") and "SENTINEL" in f for f in result.failures)

    def test_unsupported_verdict_carries_negative_contract(self, tmp_path):
        """UNSUPPORTED failures cite parent_key + absence_explanation."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\nThis claim states the SENTINEL figure.\n\n> Synthesis: inf\n\n{_PAD}",
            statements=["inf"],
        )
        result = run_validations(_UnsupportedJudge(), con, node_id, node_path)
        con.close()
        v1_failures = [f for f in result.failures if f.startswith("V1:")]
        assert v1_failures
        assert any("parent_key" in f for f in v1_failures)
        assert any("absence_explanation" in f for f in v1_failures)

    def test_unsupported_failure_is_fatal(self, tmp_path):
        """V1 UNSUPPORTED carries the fatal severity tag."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\nThis claim states the SENTINEL figure.\n\n> Synthesis: inf\n\n{_PAD}",
            statements=["inf"],
        )
        result = run_validations(_UnsupportedJudge(), con, node_id, node_path)
        con.close()
        assert any("[severity=fatal]" in f for f in result.failures)

    def test_v1_missing_link_in_synthesis_is_unsupported(self, tmp_path):
        con, node_id, node_path = _setup(
            tmp_path,
            tier="synthesis",
            prose=f"# S\n\nA source-derived fact without any link.\n\n> Synthesis: inf\n\n{_PAD}",
            statements=["inf"],
        )
        result = run_validations(_MissingLinkJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert any(
            f.startswith("V1:") and "Unsupported claim" in f and "missing declaration" in f
            for f in result.failures
        )

    def test_v1_claim_linked_to_non_supporting_parent_fails(self, tmp_path):
        """Cross-source confusion: a claim linked to parent A but stating a
        fact that lives in parent B → UNSUPPORTED against the linked parent."""
        con, node_id, node_path = _setup(
            tmp_path,
            tier="synthesis",
            prose=(
                "# S\n\n"
                "Claim A mentions TOKEN-ALPHA [[p-a|A]]. "
                "Claim B states TOKEN-BETA [[p-a|A]].\n\n"
                f"> Synthesis: inf\n\n{_PAD}"
            ),
            statements=["inf"],
            parents={
                "p-a.md": "Parent A mentions TOKEN-ALPHA here. " * 5,
                "p-b.md": "Parent B mentions TOKEN-BETA here. " * 5,
            },
        )
        result = run_validations(_LinkedParentJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        # The TOKEN-BETA claim (linked to p-a, which lacks it) is flagged…
        assert any(f.startswith("V1:") and "TOKEN-BETA" in f for f in result.failures)
        # …while the TOKEN-ALPHA claim (linked to p-a, which has it) is
        # grounded and passes D7.
        assert not any("TOKEN-ALPHA" in f and "Unsupported" in f for f in result.failures)
        assert not any(f.startswith("D7:") for f in result.failures)

    def test_d7_fabricated_hint_goes_draft(self, tmp_path):
        """D7: a SUPPORTED verdict whose evidence_hint is fabricated (absent
        from the source) cannot ground the claim → fatal D7 failure (I6)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                f"# Note\n\n{_PARENT_SENTENCE_2}\n\n"
                f"> Synthesis: inf\n"
            ),
            statements=["inf"],
        )
        result = run_validations(_FabricatedHintJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        d7_failures = [f for f in result.failures if f.startswith("D7:")]
        assert d7_failures
        assert any("[severity=fatal]" in f for f in d7_failures)
        assert any("not grounded" in f for f in d7_failures)

    def test_d7_claim_absent_from_parent_goes_draft(self, tmp_path):
        """I1: a SUPPORTED verdict on a claim the cited parent does not
        state is D7-fatal (regression of today's D7 — the judge's yes is
        not enough; the system must find the claim in the parent)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "The parser normalizes unicode entities aggressively.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": "The database exports the full ledger each evening. " * 4},
        )
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        d7_failures = [f for f in result.failures if f.startswith("D7:")]
        assert d7_failures
        assert any("[severity=fatal]" in f for f in d7_failures)
        assert any("not grounded" in f for f in d7_failures)

    def test_d7_hint_in_parent_but_claim_absent_goes_draft(self, tmp_path):
        """I1 regression of today's D7: the judge supplies an evidence_hint
        that IS a verbatim parent sentence (the old anchor check would
        auto-verify), yet the CLAIM itself is absent from the parent — the
        grounding gate must still draft the node."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "The parser normalizes unicode entities aggressively.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": "The database exports the full ledger each evening. " * 4},
        )
        judge = _SupportedJudge()

        def _call(prompt, *, allow_read=False):
            # A genuine locator (it IS in the parent) — but the claim's
            # words are not covered by the passage it points at.
            return json.dumps(
                {
                    "verdicts": [
                        _supported(
                            1,
                            "P1",
                            hint="The database exports the full ledger each evening.",
                        )
                    ]
                }
            )

        judge.call_llm = _call  # type: ignore[method-assign]
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is False
        d7_failures = [f for f in result.failures if f.startswith("D7:")]
        assert any("[severity=fatal]" in f and "not grounded" in f for f in d7_failures)
        assert result.evidence == []

    def test_supported_without_hint_grounds_claim(self, tmp_path):
        """A SUPPORTED verdict without any evidence_hint is no longer a
        failure by itself: the claim text becomes the candidate, so a claim
        genuinely covered by the parent still auto-verifies."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        judge = _SupportedJudge()

        def _call(prompt, *, allow_read=False):
            return json.dumps({"verdicts": [_supported(1, "P1")]})

        judge.call_llm = _call  # type: ignore[method-assign]
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        assert result.evidence, "a grounded SUPPORTED claim must record evidence"

    def test_supported_with_real_hint_grounds_claim(self, tmp_path):
        """A SUPPORTED verdict with a genuine evidence_hint locator grounds:
        the hint points at the parent passage that token-covers the claim."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_1}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        judge = _SupportedJudge()

        def _call(prompt, *, allow_read=False):
            # The hint is a partial leading fragment of the parent passage
            # that states the claim (a locator, not the evidence itself).
            return json.dumps(
                {
                    "verdicts": [
                        _supported(
                            1,
                            "P1",
                            hint="The memex system derives notes",
                        )
                    ]
                }
            )

        judge.call_llm = _call  # type: ignore[method-assign]
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        assert len(result.evidence) == 1
        assert result.evidence[0]["claim_index"] == 1
        assert result.evidence[0]["parent_key"] == "P1"
        assert result.evidence[0]["resolver"] == "deterministic"

    def test_d7_uncorrelated_claim_index_goes_draft_without_record(self, tmp_path):
        """A SUPPORTED verdict whose claim_index does not correlate to a
        presented slice has no claim text to gate → D7 fatal, no record
        (never a crash)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                f"# Note\n\n{_PARENT_SENTENCE_2} {_PARENT_SENTENCE_3}.\n\n"
                f"> Synthesis: inf\n"
            ),
            statements=["inf"],
        )
        judge = _SupportedJudge()

        def _call(prompt, *, allow_read=False):
            return json.dumps(
                {
                    "verdicts": [
                        _supported(1, "P1"),
                        _supported(999, "P1"),  # no such presented claim
                    ]
                }
            )

        judge.call_llm = _call  # type: ignore[method-assign]
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is False
        d7_failures = [f for f in result.failures if f.startswith("D7:")]
        assert any("[severity=fatal]" in f and "no claim text" in f for f in d7_failures)
        # The stray verdict produced no record; only the grounded claim did.
        assert [r["claim_index"] for r in result.evidence] == [1]

    def test_d7_resolves_source_when_parent_key_invalid(self, tmp_path):
        """D7 fallback: a SUPPORTED verdict with a bad parent_key is still
        grounded when the claim is genuinely covered by a readable parent —
        the claim/hint, not the key, resolves the cited source."""
        con, node_id, node_path = _setup(
            tmp_path,
            tier="synthesis",
            prose=(
                "# S\n\n"
                "The database exports the full ledger each evening "
                "[[p-a|A]].\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"p-a.md": "The database exports the full ledger each evening. " * 6},
        )
        judge = _SupportedJudge()

        def _call(prompt, *, allow_read=False):
            if "Synthesis statements:" in prompt:
                return json.dumps({"passes": True, "reason": "ok"})
            # An out-of-range parent key; the hint is the reliable pointer.
            return json.dumps(
                {
                    "verdicts": [
                        _supported(
                            1,
                            "P99",
                            hint="The database exports the full ledger each evening.",
                        )
                    ]
                }
            )

        judge.call_llm = _call  # type: ignore[method-assign]
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        assert not any(f.startswith("D7:") for f in result.failures)
        assert result.evidence[0]["parent_key"] == "P1"

    def test_v1_fatal_skips_v2(self, tmp_path):
        """DAG: V1 fatal failures → V2 never called (single judge turn)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\nThis claim states the SENTINEL figure.\n\n> Synthesis: inf\n\n{_PAD}",
            statements=["inf"],
        )
        judge = _CountingJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert judge.calls == 1, f"Expected exactly 1 judge call (V1 only), got {judge.calls}"
        assert any(f.startswith("V1:") for f in result.failures)
        assert not any(f.startswith("V2:") for f in result.failures)

    def test_v2_runs_when_v1_clean(self, tmp_path):
        """DAG: V1 clean → V2 runs (two judge turns) and passes."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                f"# Note\n\n{_PARENT_SENTENCE_2}\n\n"
                f"> Synthesis: inf\n"
            ),
            statements=["inf"],
        )
        judge = _CountingJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is True
        assert judge.calls == 2, f"Expected V1 + V2 calls, got {judge.calls}"

    def test_v2_boilerplate_fails_with_quality_severity(self, tmp_path):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: SENTINEL-BOILERPLATE restatement\n",
            statements=["SENTINEL-BOILERPLATE restatement"],
        )
        result = run_validations(_BoilerplateJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        v2_failures = [f for f in result.failures if f.startswith("V2:")]
        assert v2_failures
        assert any("[severity=quality]" in f for f in v2_failures)

    def test_judge_raising_warns_and_skips(self, tmp_path, capsys):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        result = run_validations(_RaisingJudge(), con, node_id, node_path)
        con.close()
        err = capsys.readouterr().err
        assert "validation_warning" in err
        assert result.passed is True
        assert result.failures == []

    def test_judge_garbage_warns_and_skips(self, tmp_path, capsys):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        result = run_validations(_GarbageJudge(), con, node_id, node_path)
        con.close()
        err = capsys.readouterr().err
        assert "validation_warning" in err
        assert result.passed is True
        assert result.failures == []

    def test_judge_without_call_llm_warns_and_passes(self, tmp_path, capsys):
        """F1: a judge without a call_llm seam (DemoAgent — the default
        first-party agent) skips the V1/V2 family WITH a validation_warning:
        the always-on quality gate is never silently disabled. The
        deterministic checks D1–D6 remain the gate, so the node still
        passes."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        result = run_validations(DemoAgent(), con, node_id, node_path)
        con.close()
        err = capsys.readouterr().err
        assert "validation_warning" in err
        assert "call_llm" in err
        assert result.passed is True
        assert result.failures == []

    def test_reader_judge_receives_path_references(self, tmp_path):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        judge = _ReaderJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is True
        assert judge.captured["allow_read"] is True
        assert judge.captured["has_path"] is True

    def test_parentless_node_validation_passes_vacuously(self, tmp_path):
        """No provenance parents → nothing to ground against (D1 gates it)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        con.execute("DELETE FROM edge WHERE from_node = ?", (node_id,))
        con.commit()
        result = run_validations(_UnsupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is True
        assert result.failures == []

    def test_content_read_failure_is_fatal(self, tmp_path):
        """F3: an unreadable content file (deleted after the node was
        created) is a FATAL validation failure — run_validations never
        passes a node whose evidence cannot be read."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        node_path.unlink()
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert any(
            "[severity=fatal]" in f and "Validation content read failed" in f
            for f in result.failures
        )

    def test_invalid_utf8_parent_content_degrades_not_crashes(self, tmp_path):
        """F1: a parent content file with invalid UTF-8 bytes (latin-1
        scraped pages, binary blobs placed in the vault) must not raise
        UnicodeDecodeError out of run_validations — the parent degrades to
        content=None ('content unavailable'), D7 cannot resolve the claim
        and drafts the node, and the derive/synthesize call completes with
        a normal CheckResult instead of a traceback."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
            parents={"parent.md": "placeholder"},
        )
        (tmp_path / "parent.md").write_bytes(b"\xff\xfe\x80 invalid utf-8 bytes")
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        # No UnicodeDecodeError propagates; the failure is a normal
        # deterministic D7 draft (claim unresolvable against missing
        # content) — never an exception that aborts the derive.
        assert result.passed is False
        assert any(
            "D7:" in f and "[severity=fatal]" in f
            for f in result.failures
        )

    def test_invalid_utf8_node_content_is_fatal_read_failure(self, tmp_path):
        """F1: the node-content read site catches UnicodeDecodeError the
        same way as OSError — invalid UTF-8 in the node's own markdown is
        a fatal 'content read failed' CheckResult, never a crash."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        node_path.write_bytes(b"# Note\n\nThis claim has \xff\x80 invalid bytes.\n")
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert any(
            "[severity=fatal]" in f and "Validation content read failed" in f
            for f in result.failures
        )


class TestEvidenceRecords:
    """I3 + I5 at the DAG level: records exist exactly for grounded
    SUPPORTED claims, with verbatim spans (I4)."""

    def test_all_supported_records_one_evidence_per_claim(self, tmp_path):
        from memex.validators.evidence import normalize_surface

        prose = (
            "# Note\n\n"
            f"{_PARENT_SENTENCE_1} {_PARENT_SENTENCE_2} "
            f"{_PARENT_SENTENCE_3}.\n\n"
            "> Synthesis: inf\n"
        )
        con, node_id, node_path = _setup(
            tmp_path, prose=prose, statements=["inf"],
        )
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        # One record per grounded SUPPORTED claim (3 claims, 3 records).
        assert [r["claim_index"] for r in result.evidence] == [1, 2, 3]
        norm = normalize_surface(_DEFAULT_PARENT)
        for record in result.evidence:
            assert set(record) == {
                "claim_index", "parent_key", "span_text", "confidence", "resolver",
            }
            assert record["parent_key"] == "P1"
            assert record["resolver"] == "deterministic"
            assert record["confidence"] == 1.0
            # I4: the persisted span is a verbatim substring of the
            # normalized parent content — the resolver selects, never
            # generates.
            assert record["span_text"] in norm

    def test_unsupported_never_produces_evidence_record(self, tmp_path):
        """I3: an UNSUPPORTED verdict is never resolved and never produces
        an evidence record; its failure is V1's negative-verdict contract
        only."""
        prose = (
            "# Note\n\n"
            "This claim states the SENTINEL figure. "
            f"{_PARENT_SENTENCE_2}\n\n"
            "> Synthesis: inf\n"
        )
        con, node_id, node_path = _setup(
            tmp_path, prose=prose, statements=["inf"],
        )
        result = run_validations(_UnsupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert any(f.startswith("V1:") for f in result.failures)
        # Only the grounded SUPPORTED claim (index 2) recorded evidence.
        assert [r["claim_index"] for r in result.evidence] == [2]

    def test_unsupported_groundable_claim_never_records(self, tmp_path):
        """I3 anti-vacuity: the UNSUPPORTED verdict here cites a claim the
        parent token-covers (the deterministic resolver WOULD ground and
        record it if the verdict were SUPPORTED — 6 of the claim's 7
        content tokens sit in the parent). The record list must stay empty
        because the verdict is UNSUPPORTED, not because the claim is
        ungroundable: this test FAILS if UNSUPPORTED verdicts were ever
        resolved/recorded."""
        prose = (
            "# Note\n\n"
            "The memex system derives SENTINEL notes from source documents.\n\n"
            "> Synthesis: inf\n"
        )
        con, node_id, node_path = _setup(
            tmp_path, prose=prose, statements=["inf"],
        )
        result = run_validations(_UnsupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert any(f.startswith("V1:") for f in result.failures)
        assert result.evidence == []

    def test_common_knowledge_pass_records_nothing(self, tmp_path):
        """I5 negative arm: a COMMON_KNOWLEDGE verdict is never resolved —
        a passing node whose claim text the parent token-covers still
        records no evidence (the deterministic resolver would ground it if
        the verdict were SUPPORTED)."""
        prose = (
            "# Note\n\n"
            f"{_PARENT_SENTENCE_2}\n\n"
            "> Synthesis: inf\n"
        )
        con, node_id, node_path = _setup(
            tmp_path, prose=prose, statements=["inf"],
        )
        result = run_validations(_CommonKnowledgeJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        assert result.evidence == []

    def test_duplicate_supported_verdict_records_once(self, tmp_path):
        """I5: a judge emitting TWO SUPPORTED verdicts for the same
        claim_index (both groundable) yields exactly ONE evidence record
        for that claim — duplicate verdicts never multiply records for a
        single grounded claim."""
        prose = (
            "# Note\n\n"
            f"{_PARENT_SENTENCE_2}\n\n"
            "> Synthesis: inf\n"
        )
        con, node_id, node_path = _setup(
            tmp_path, prose=prose, statements=["inf"],
        )
        result = run_validations(_DuplicateVerdictJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        assert [r["claim_index"] for r in result.evidence] == [1]

    def test_ungrounded_supported_never_records(self, tmp_path):
        """A SUPPORTED verdict that fails the gate records nothing."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "The parser normalizes unicode entities aggressively.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": "The database exports the full ledger each evening. " * 4},
        )
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert result.evidence == []


class _EmptyVerdictsJudge(_SupportedJudge):
    """V1 returns a clean but empty verdict set — a silent no-op otherwise."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        return json.dumps({"verdicts": []})


class _PartialVerdictsJudge(_SupportedJudge):
    """V1 returns verdicts for only the first presented claim."""

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return self._v2(prompt)
        slices = FakeJudge._presented_slices(prompt)
        if not slices:
            return json.dumps({"verdicts": []})
        s = slices[0]
        return json.dumps({"verdicts": [_supported(s["index"], s["key"])]})


class _BoolishPassJudge(_SupportedJudge):
    """V2 emits {passes: <bool-ish value>} — LLM JSON sloppiness."""

    def __init__(self, passes_value: object) -> None:
        self.passes_value = passes_value

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            return json.dumps(
                {"passes": self.passes_value, "reason": "ok"}
            )
        return super().call_llm(prompt, allow_read=allow_read)


class TestSlicerSkipsNonProse:
    """F2: only unadorned prose becomes V1 claims — fenced code blocks,
    tables, list bullets and blockquotes must not leak into the claims."""

    def test_fenced_code_tables_lists_blockquotes_are_stripped(self):
        from memex.rules import _split_claims, _unadorned_prose

        content = (
            "# Note\n\n"
            "A real claim about the subject.\n\n"
            "```python\n"
            "x = 1\n"
            "y = 2\n"
            "```\n\n"
            "| Header | Value |\n"
            "|---|---|\n"
            "| a | b |\n\n"
            "- a bullet point\n"
            "1. numbered item\n"
            "> a quoted aside\n\n"
            "Another real claim here.\n\n"
            "> Synthesis: inf\n"
        )
        claims = _split_claims(_unadorned_prose(content))
        assert claims == [
            "A real claim about the subject.",
            "Another real claim here.",
        ]

    def test_unclosed_fence_drops_remainder(self):
        """An unclosed fence drops the trailing lines too (the safe
        direction: trailing code must never leak into the claims)."""
        from memex.rules import _split_claims, _unadorned_prose

        content = (
            "# Note\n\n"
            "A real claim about the subject.\n\n"
            "```python\n"
            "x = 1\n"
            "trailing code line\n"
        )
        claims = _split_claims(_unadorned_prose(content))
        assert claims == ["A real claim about the subject."]

    def test_indented_code_blocks_are_stripped(self):
        """F3: Markdown 4-space-indented code blocks are not claims either —
        a [[ghost|...]] inside an indented code example is code, not a
        wikilink declaration (same stripping D6 applies), and the indented
        lines never reach the judge."""
        from memex.rules import _split_claims, _unadorned_prose

        content = (
            "# Note\n\n"
            "A real claim about the subject.\n\n"
            '    link = "[[ghost|Ghost]]"\n'
            '    more = "[[ghost2|Ghost2]]"\n'
            "\n"
            "Another real claim here.\n\n"
            "> Synthesis: inf\n"
        )
        claims = _split_claims(_unadorned_prose(content))
        assert claims == [
            "A real claim about the subject.",
            "Another real claim here.",
        ]
        assert not any("ghost" in c for c in claims)


class TestVerdictShortfallWarning:
    """F3: V1 verdict shortfalls (empty or partial sets) warn — an
    incomplete grounding pass is never silently clean."""

    def test_empty_verdict_set_warns_not_silent(self, tmp_path, capsys):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        result = run_validations(_EmptyVerdictsJudge(), con, node_id, node_path)
        con.close()
        err = capsys.readouterr().err
        assert "validation_warning" in err
        assert "shortfall" in err
        # Degrade to pass-with-warning (the DAG never fails on coverage).
        assert result.passed is True

    def test_partial_verdict_set_warns(self, tmp_path, capsys):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                f"{_PARENT_SENTENCE_2} {_PARENT_SENTENCE_3}.\n\n"
                f"> Synthesis: inf\n"
            ),
            statements=["inf"],
        )
        result = run_validations(_PartialVerdictsJudge(), con, node_id, node_path)
        con.close()
        err = capsys.readouterr().err
        assert "shortfall" in err
        assert "were not judged" in err
        assert result.passed is True

    def test_zero_claims_from_list_only_body_warns(self, tmp_path, capsys):
        """F2: a body made ENTIRELY of list items yields zero V1 claims
        (_unadorned_prose strips every line) — the grounding wave is
        skipped with a validation_warning, never silently (the one
        incomplete-coverage case with no verdict set to count)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "- First bullet\n"
                "- Second bullet\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
        )
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        err = capsys.readouterr().err
        assert "validation_warning" in err
        assert "no claims to judge" in err
        assert result.passed is True


class _BothPromptsCapturingJudge(_PromptCapturingJudge):
    """Records the V1 and V2 prompts (V1 prompt recording is inherited)."""

    def __init__(self) -> None:
        super().__init__()
        self.v2_prompt: str | None = None

    def call_llm(self, prompt: str, *, allow_read: bool = False) -> str:
        if "Synthesis statements:" in prompt:
            if self.v2_prompt is None:
                self.v2_prompt = prompt
            return self._v2(prompt)
        return super().call_llm(prompt, allow_read=allow_read)


class TestTemplateFillingImmuneToPlaceholderText:
    """F2: _fill_template substitutes in a single pass — literal
    "{v1_verdicts}" / "{parents}" / "{slices}" text inside a parent file or
    the node body is preserved verbatim in the judge prompt, never
    clobbered by a later sequential fill rewriting the judge's evidence."""

    def test_parent_and_body_placeholder_text_is_preserved(self, tmp_path):
        parent_content = (
            "Parent content: this file mentions {v1_verdicts} and {slices} "
            "and {parents} as literal text. " * 2
        )
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "The file mentions the literal placeholder text. "
                "{v1_verdicts} and {parents} appear as literal text.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": parent_content},
        )
        judge = _BothPromptsCapturingJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert judge.v1_prompt is not None
        assert judge.v2_prompt is not None
        # V1 prompt: the inlined parent block keeps every literal
        # placeholder — the old sequential fill would have replaced
        # "{slices}" and "{parents}" with rendered blocks here.
        assert (
            "mentions {v1_verdicts} and {slices} and {parents} as literal text"
            in judge.v1_prompt
        )
        # V2 prompt: same parent block, plus the node body's "{parents}"
        # surviving verbatim (old code replaced it with the parents block).
        assert (
            "mentions {v1_verdicts} and {slices} and {parents} as literal text"
            in judge.v2_prompt
        )
        assert "{v1_verdicts} and {parents} appear as literal text." in judge.v2_prompt
        # The judge still judged normally — preservation, not corruption.
        assert result.passed is True, result.failures


class TestV1PromptNotesExemption:
    """F1: the V1 prompt exempts notes-tier claims from the link-named-parent
    judgment — in a notes derivation every claim is judged against the
    single parent regardless of any inline links it carries (a stray
    [[ghost|...]] link never redirects the judgment to a parent that does
    not exist in the Parent content section)."""

    def test_notes_tier_prompt_includes_the_exemption_clause(self, tmp_path):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                f"{_PARENT_SENTENCE_2} The source material covers the "
                "subject thoroughly [[ghost|Ghost]].\n\n"
                f"> Synthesis: inf\n"
            ),
            statements=["inf"],
        )
        judge = _PromptCapturingJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert judge.v1_prompt is not None
        assert "regardless of any inline links" in judge.v1_prompt
        # The judge follows the exemption: every claim — despite its stray
        # link — is SUPPORTED against the single parent, so the node
        # auto-verifies (no ghost parent exists to judge against).
        assert result.passed is True, result.failures


class TestParentPromptCap:
    """F5: parent content inlined into judge prompts is NUL-stripped and
    size-capped like the derive path's source content; D7's evidence
    resolution still sees the FULL content (the resolver runs on the full
    local content, not the capped prompt copy)."""

    def test_oversized_parent_inline_is_capped_but_d7_sees_full(self, tmp_path):
        from memex.utils.parsing import _MAX_PROMPT_CHARS

        parent_content = (
            "Parent content prefix. " + "\x00" * 10 + "x" * 300_000
            + " TAIL-MARKER-QUOTE caps the file."
        )
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "Parent content prefix. TAIL-MARKER-QUOTE caps the file.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": parent_content},
        )
        judge = _TailMarkerJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert judge.v1_prompt is not None
        parents_block = judge.v1_prompt.split("Parent P1:", 1)[1]
        # Inlined parent content is capped: the tail beyond the cap and the
        # NUL bytes never reach the judge.
        assert "TAIL-MARKER-QUOTE" not in parents_block
        assert "\x00" not in parents_block
        assert "[source content truncated" in parents_block
        assert len(judge.v1_prompt) < _MAX_PROMPT_CHARS + 5_000
        # D7 resolves against the full local content: the tail claim is
        # token-covered there, so the node still auto-verifies.
        assert result.passed is True, result.failures


class TestD7NulByteResolution:
    """F3: parent content is NUL-stripped at load (_load_parents), so D7's
    evidence resolution runs against the same surface the judge saw (the
    prompt copy is NUL-stripped by _cap_prompt_content). A judge whose
    claim/hint spans a NUL byte (PDF ToUnicode artifact) in the raw file
    must still ground — the NUL-stripped surface carries the words."""

    def test_claim_spanning_nul_byte_grounds_against_stripped_parent(
        self, tmp_path
    ):
        raw = b"NUL \x00 parent content supporting the claim text. " * 4
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "NUL parent content supporting the claim text.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": "placeholder"},
        )
        (tmp_path / "parent.md").write_bytes(raw)
        result = run_validations(_SupportedJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        assert not any(f.startswith("D7:") for f in result.failures)

    def test_reader_judge_nul_spanning_hint_grounds(self, tmp_path):
        """F3: D7 is surface-invariant for READER judges — the default
        production judge reads the RAW parent file, so a SUPPORTED verdict
        whose evidence_hint spans a PDF ToUnicode NUL byte (echoed from the
        raw read) must still resolve against the NUL-stripped local content
        with no spurious fatal."""
        raw = b"NUL \x00 parent content supporting the claim text. " * 4
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "NUL parent content supporting the claim text.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": "placeholder"},
        )
        (tmp_path / "parent.md").write_bytes(raw)
        judge = _NulEchoingReaderJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        # The reader surface was exercised: path references, not inlined
        # content — the judge echoed the raw file's NUL byte in its hint.
        assert judge.reader_surface is True
        assert result.passed is True, result.failures
        assert not any(f.startswith("D7:") for f in result.failures)

    def test_common_knowledge_on_link_free_synthesis_fails(self, tmp_path):
        """F6: COMMON_KNOWLEDGE is the third verdict path — the deterministic
        net backstops it: a link-free synthesis claim marked COMMON_KNOWLEDGE
        is a missing declaration and fails; notes-tier CK (no link contract)
        still passes."""
        con, node_id, node_path = _setup(
            tmp_path,
            tier="synthesis",
            prose=(
                "# S\n\n"
                "A generic-sounding source-derived fact without any link.\n\n"
                f"> Synthesis: inf\n\n{_PAD}"
            ),
            statements=["inf"],
        )
        result = run_validations(_CommonKnowledgeJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is False
        d7 = [f for f in result.failures if f.startswith("D7:")]
        assert d7
        assert any("COMMON_KNOWLEDGE" in f and "missing declaration" in f for f in d7)
        assert any("[severity=fatal]" in f for f in d7)

    def test_common_knowledge_on_notes_passes(self, tmp_path):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n",
            statements=["inf"],
        )
        result = run_validations(_CommonKnowledgeJudge(), con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures


class TestNormalizedSurfaceTolerance:
    """D7's resolver is surface-invariant: extraction spacing/unicode/entity
    artifacts fold away before token comparison — the old literal-quote
    helpers' guarantees now live in normalize_surface/content_tokens
    (unit-tested in test_evidence_resolution.py); these pin the behavior at
    the DAG level."""

    def test_claim_with_lookalike_surface_grounds(self, tmp_path):
        # The parent carries a curly apostrophe and a math-italic 'w'; the
        # claim writes the straight forms — same folded surface.
        from memex.validators.evidence import normalize_surface

        parent = "the model\u2019s ability to recall, with weight \U0001d464 " * 4
        con, node_id, node_path = _setup(
            tmp_path,
            prose=(
                "# Note\n\n"
                "The model's ability to recall is measured by weight.\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"parent.md": parent},
        )
        judge = _SupportedJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is True, result.failures
        norm = normalize_surface(parent)
        assert result.evidence[0]["span_text"] in norm


class TestParentPromptAggregateCap:
    """F1: the inline parent cap is CUMULATIVE across parents — a synthesis
    over several large parents must not concatenate N near-cap blocks into
    a prompt that overflows the judge's window (which silently skipped both
    waves with a stderr warning). The aggregate budget is allocated across
    parents, the joined block carries the truncation note, and the V1/V2
    waves still run."""

    def test_joined_large_parents_capped_at_aggregate_budget(self, tmp_path, capsys):
        from memex.utils.parsing import _MAX_PROMPT_CHARS

        big = "Parent content prefix. " + "y" * 89_000
        con, node_id, node_path = _setup(
            tmp_path,
            tier="synthesis",
            prose=(
                "# S\n\n"
                "Parent content prefix appears in [[p-a|A]].\n"
                "Parent content prefix appears in [[p-b|B]].\n\n"
                "> Synthesis: inf\n"
            ),
            statements=["inf"],
            parents={"p-a.md": big, "p-b.md": big},
        )
        judge = _CountingSupportedJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert judge.v1_prompt is not None
        # The joined parent content is capped at the aggregate budget — NOT
        # at N times the per-parent cap (2 x ~89k chars ≈ 178k pre-fix).
        assert len(judge.v1_prompt) < _MAX_PROMPT_CHARS + 5_000
        # Every sliced parent carries the truncation note, so the judge
        # knows content was elided.
        assert judge.v1_prompt.count("[source content truncated") == 2
        # Both V1 and V2 waves ran — an oversized prompt must never silently
        # skip the always-on grounding/re-elaboration gate.
        assert judge.calls == 2
        assert result.passed is True, result.failures
        err = capsys.readouterr().err
        assert "validation_warning" not in err


class TestNodeSidePromptBudget:
    """F1: the node side of the judge prompt (slices + body) is budgeted
    alongside the parents — a D4-legal synthesis body near the 150k ceiling
    (with a parent block near the aggregate cap) must not overflow the
    judge's window, which previously raised in the judge call and silently
    skipped both waves with a stderr warning."""

    def test_large_body_with_oversized_parents_stays_in_budget(
        self, tmp_path, capsys
    ):
        from memex.utils.parsing import _MAX_PROMPT_CHARS

        big = "Parent content prefix. " + "y" * 89_000
        # D4-legal synthesis body (>100k chars; the synthesis ceiling is
        # 150k) whose bulk is a fenced code region: _unadorned_prose strips
        # it, so V1 still judges a small claim set while V2's {body}
        # placeholder embeds the FULL size — the pre-fix prompt (body
        # ~120k + parents ~178k ≈ 300k) overflowed the judge's window.
        body = (
            "# S\n\n"
            "Parent content prefix appears in [[p-a|A]].\n"
            "Parent content prefix appears in [[p-b|B]].\n\n"
            "> Synthesis: inf\n\n"
            "```\n"
            + "filler-line\n" * 10_000
            + "```\n"
        )
        assert len(body) > 100_000
        con, node_id, node_path = _setup(
            tmp_path,
            tier="synthesis",
            prose=body,
            statements=["inf"],
            parents={"p-a.md": big, "p-b.md": big},
        )
        judge = _CountingSupportedJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert judge.v1_prompt is not None
        assert judge.v2_prompt is not None
        # Both the V1 prompt (slices + parents) and the V2 prompt (slices +
        # body + parents) stay within the aggregate budget.
        assert len(judge.v1_prompt) <= _MAX_PROMPT_CHARS + 5_000
        assert len(judge.v2_prompt) <= _MAX_PROMPT_CHARS + 5_000
        # Both V1 and V2 waves ran — an oversized node body must never
        # silently skip the always-on grounding/re-elaboration gate.
        assert judge.calls == 2
        assert result.passed is True, result.failures
        err = capsys.readouterr().err
        assert "validation_warning" not in err


class TestParentBlockBudget:
    """F2: the parent block's framing reservation counts the
    '(content unavailable)' suffix of every unreadable parent and is
    clamped against the budget — the joined block never exceeds it, even
    when the reservation alone (headers + notes + suffixes) would."""

    def _parents(self, n_unavailable: int, big: str) -> list[dict]:
        parents = [
            {
                "node_id": f"unreadable-{i}",
                "key": f"P{i + 1}",
                "filename": f"gone-{i}.md",
                "content_path": f"/tmp/gone-{i}.md",
                "content": None,
                "title": None,
            }
            for i in range(n_unavailable)
        ]
        parents.append(
            {
                "node_id": "big",
                "key": f"P{n_unavailable + 1}",
                "filename": "big.md",
                "content_path": "/tmp/big.md",
                "content": big,
                "title": None,
            }
        )
        return parents

    def test_unavailable_suffix_reserved_alongside_oversized_parents(self):
        from memex.utils.parsing import _MAX_PROMPT_CHARS
        from memex.validators.validate import _parent_block

        big = "Parent content prefix. " + "y" * 89_000
        parents = self._parents(n_unavailable=1_000, big=big)
        block = _parent_block(parents, allow_read=False)
        # The 22-char "(content unavailable)" suffix per unreadable parent
        # is reserved up front — the joined block (framing + sliced content
        # + notes + suffixes) stays within the aggregate budget. Pre-fix,
        # the unreserved suffixes pushed it ~22k over.
        assert len(block) <= _MAX_PROMPT_CHARS
        assert block.count("(content unavailable)") == 1_000
        assert "[source content truncated" in block

    def test_extreme_parent_count_is_clamped_to_budget(self):
        from memex.utils.parsing import _MAX_PROMPT_CHARS, _TRUNCATION_NOTE
        from memex.validators.validate import _parent_block

        # The reservation itself (headers + separators + suffixes) exceeds
        # the budget, so content_budget clamps to 0 — the headers/notes
        # total must still be bounded by the block-level clamp.
        parents = self._parents(n_unavailable=3_000, big="small content")
        block = _parent_block(parents, allow_read=False)
        assert len(block) <= _MAX_PROMPT_CHARS + len(_TRUNCATION_NOTE)
        # The clamp engages and explains the elision.
        assert "[source content truncated" in block


class TestSharedSynthesisStatementsParse:
    """F2: the synthesis_statements column parse is ONE shared helper —
    _decode_statements and D3's check both use parse_synthesis_statements,
    with identical semantics (valid JSON array, garbage, non-list, str
    coercion, null/empty)."""

    def test_valid_json_array(self):
        from memex.utils.parsing import parse_synthesis_statements

        assert parse_synthesis_statements('["a", "b"]') == ["a", "b"]
        # Elements are str-coerced, matching both call sites.
        assert parse_synthesis_statements('[1, 2]') == ["1", "2"]

    def test_garbage_and_non_list_tolerated(self):
        from memex.utils.parsing import parse_synthesis_statements

        assert parse_synthesis_statements("not json") == []
        assert parse_synthesis_statements("{}") == []
        assert parse_synthesis_statements("42") == []

    def test_empty_and_null_tolerated(self):
        from memex.utils.parsing import parse_synthesis_statements

        assert parse_synthesis_statements(None) == []
        assert parse_synthesis_statements("") == []
        assert parse_synthesis_statements("[]") == []

    def test_decode_statements_delegates_to_shared_helper(self):
        from memex.utils.parsing import parse_synthesis_statements
        from memex.validators.validate import _decode_statements

        for raw in (None, "", "not json", "{}", "42", '["a", 1]'):
            assert _decode_statements(raw) == parse_synthesis_statements(raw)


class TestSliceClaimTextEmbeddedQuotes:
    """F2: _slice_claim_text extracts the claim by its TRAILING delimiter —
    embedded double quotes inside the claim text are part of the claim, not
    the end of it (a first-quote extraction would key the verdict against a
    truncated prefix and produce spurious coverage gaps / a weakened
    presented-claim guarantee)."""

    def test_single_line_claim_with_embedded_quote_round_trips(self):
        from memex.validators.validate import _slice_claim_text

        block = 'Claim 1: "The author wrote "hello" to the editor."'
        assert _slice_claim_text(block) == 'The author wrote "hello" to the editor.'

    def test_embedded_quote_before_links_suffix_round_trips(self):
        from memex.validators.validate import _slice_claim_text

        block = (
            'Claim 2: "Alpha wrote "hello" to [[p-a|A]]."\n'
            "  links: [[p-a|A]] -> parent p-a"
        )
        assert _slice_claim_text(block) == 'Alpha wrote "hello" to [[p-a|A]].'

    def test_multiline_claim_with_embedded_quote_not_truncated(self):
        """An embedded quote followed by a newline inside the claim must not
        end the extraction: the claim is everything up to the CLOSING quote,
        never a prefix cut at the first quote with a line break after it."""
        from memex.validators.validate import _slice_claim_text

        block = 'Statement 1: "He asked "who is there?"\nNo one answered."'
        assert _slice_claim_text(block) == (
            'He asked "who is there?"\nNo one answered.'
        )

    def test_unexpected_shape_falls_back_to_whole_block(self):
        from memex.validators.validate import _slice_claim_text

        assert _slice_claim_text("not a slice") == "not a slice"
        assert _slice_claim_text('Claim 1: "no closing quote') == (
            'Claim 1: "no closing quote'
        )


class TestV2PassesCoercion:
    """F4: V2 passes is coerced bool-ish like V1 normalizes verdicts."""

    def test_boolish_passes_values_are_accepted(self, tmp_path):
        for value in ("true", "TRUE", "1", 1, 1.0, True, "yes", "on"):
            con, node_id, node_path = _setup(
                tmp_path,
                prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n\n{_PAD}",
                statements=["inf"],
            )
            result = run_validations(_BoolishPassJudge(value), con, node_id, node_path)
            con.close()
            assert result.passed is True, f"passes={value!r} should pass"

    def test_false_passes_still_fails(self, tmp_path):
        for value in (False, "false", 0, 0.0, "0"):
            con, node_id, node_path = _setup(
                tmp_path,
                prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n\n{_PAD}",
                statements=["inf"],
            )
            result = run_validations(_BoolishPassJudge(value), con, node_id, node_path)
            con.close()
            assert result.passed is False, f"passes={value!r} should fail"
            assert any(
                f.startswith("V2:") and "[severity=quality]" in f
                for f in result.failures
            )


class TestDeclarativeDag:
    """F7: ordering, dependencies and skip conditions live on the rules —
    run_validations drives the DAG from the registry."""

    def test_registry_declares_the_dag(self):
        from memex.rules import VALIDATION_RULES

        by_id = {r.id: r for r in VALIDATION_RULES}
        v1, v2 = by_id["V1"], by_id["V2"]
        assert v1.order < v2.order
        assert v1.depends_on == ()
        assert v1.expects_full_verdicts is True
        assert v2.depends_on == ("V1",)
        assert v2.skip_when_fatal is True

    def test_v1_fatal_still_skips_v2(self, tmp_path):
        """The skip condition is registry-driven: V2.skip_when_fatal keeps the
        DAG edge (V1 fatal → no V2 judge call)."""
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\nThis claim states the SENTINEL figure.\n\n> Synthesis: inf\n\n{_PAD}",
            statements=["inf"],
        )
        judge = _CountingJudge()
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is False
        assert judge.calls == 1, f"Expected exactly 1 judge call (V1 only), got {judge.calls}"
        assert not any(f.startswith("V2:") for f in result.failures)


class TestNegativeVerdictContract:
    """F11: UNSUPPORTED verdicts must cite parent_key +
    absence_explanation — a judge omitting either fails deterministically."""

    def test_unsupported_without_contract_fields_fails_deterministically(
        self, tmp_path
    ):
        con, node_id, node_path = _setup(
            tmp_path,
            prose=f"# Note\n\n{_PARENT_SENTENCE_2}\n\n> Synthesis: inf\n\n{_PAD}",
            statements=["inf"],
        )
        judge = _SupportedJudge()

        def _call(prompt, *, allow_read=False):
            return json.dumps(
                {"verdicts": [{"claim_index": 1, "verdict": "UNSUPPORTED"}]}
            )

        judge.call_llm = _call  # type: ignore[method-assign]
        result = run_validations(judge, con, node_id, node_path)
        con.close()
        assert result.passed is False
        v1_failures = [f for f in result.failures if f.startswith("V1:")]
        assert v1_failures
        assert any("negative-verdict contract violated" in f for f in v1_failures)
        assert any("[severity=fatal]" in f for f in v1_failures)
