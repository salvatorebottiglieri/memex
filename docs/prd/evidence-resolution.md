# PRD: Deterministic evidence resolution (reference-based grounding)

## Problem statement

The V1 judge emits `evidence_anchor` — a text string the LLM *generates*. An LLM
that generates text paraphrases, so a fraction of anchors fail D7's verbatim
check even when the claim is genuinely grounded. The principle being applied is:

> **LLMs must not emit change-sensitive objects; they reference them, and the
> system inserts/resolves them deterministically.**

The link redesign already follows it (`P1..Pn` → system resolves to uuid). The
anchor does *not*: the judge still emits the evidence text, and the system only
*verifies* it. This PRD completes the principle for evidence: the judge emits
only references, and the system *resolves* the evidence from the source.

## Solution

### 1. Verdict contract (V1 output)

```json
{
  "claim_index": 3,
  "verdict": "SUPPORTED",
  "parent_key": "P2",
  "evidence_hint": "optional candidate text, may be paraphrased (≤ ~30 words)"
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `claim_index` | int (1-based) | yes | reference to the `Claim N:` slice |
| `verdict` | enum | yes | `SUPPORTED` / `COMMON_KNOWLEDGE` / `UNSUPPORTED` |
| `parent_key` | `P1..Pn` | yes (SUPPORTED/UNSUPPORTED) | reference, not text |
| `evidence_hint` | str, optional | no | a *locator*, not the evidence; may be paraphrased |
| `absence_explanation` | str | yes (UNSUPPORTED) | negative-verdict contract (unchanged) |

`evidence_hint` is declassified from "evidence" to "locator": its value is
usefulness to the resolver, not fidelity.

### 2. Evidence record

```python
Evidence:
    span_text:   str        # verbatim substring of the NORMALIZED source
    confidence:  float      # 0..1
    resolver:    "deterministic" | "extractive"
    start: int | None       # char offsets into the normalized source
    end:   int | None
```

`span_text` is verbatim *by construction* — a resolver selects spans from the
source, it never generates. Offsets are phase-2 (audit); `span_text` is the
primary artifact. Normalized surface = NUL-stripped, unicode-normalized
(NFKC + look-alike folding), HTML-entity-decoded (the same surface D7 already
matches on).

### 3. Resolver interface

```
resolve_evidence(candidate: str, source: str) -> Evidence | None
```

`None` = no confident alignment. Two implementations behind one interface:

- **`DeterministicResolver`** (default, zero dependencies) — sliding window +
  token coverage. `confidence` = coverage. Tolerant of spacing/unicode/entity
  artifacts; not tolerant of paraphrase.
- **`ExtractiveResolver`** (optional upgrade) — small span-extraction QA model
  (DistilBERT/SQuAD-class, ~250 MB). Input = (candidate as query, source as
  context); output = span. Tolerant of paraphrase; output always verbatim.

### 4. Grounding gate

The judge says SUPPORTED; the system decides.

1. `content_tokens(claim)` = numbers + non-stopword words (len ≥ 3), normalized.
2. `coverage` = fraction of `content_tokens(claim)` present in `span_text`.
3. `grounded ⇔ |content_tokens| ≥ min_tokens AND coverage ≥ threshold`.

**Defaults (locked): `threshold = 0.6`, `min_tokens = 2`.**

Fail-closed: below `min_tokens` the claim has no checkable content → ungrounded.
The gate can **falsify** a SUPPORTED verdict (judge says yes, system says no →
D7 fatal); it can **never overturn** an UNSUPPORTED verdict (semantic judgment,
stays with the judge + negative contract).

### 5. Cascade

```
candidate = evidence_hint or claim
DeterministicResolver → grounded?  ── yes ──> record evidence
        │ no
ExtractiveResolver (if configured) → grounded?  ── yes ──> record evidence
        │ no / not configured
        └──> D7 fatal (ungrounded)
```

The extractive model runs only when the deterministic resolver is under
threshold (latency economy).

### 6. Failure semantics

- `SUPPORTED` + not grounded → `D7: [severity=fatal]` (system overrides judge).
- `SUPPORTED` + no hint + no grounding → same.
- `UNSUPPORTED` + missing `parent_key`/`absence_explanation` → `V1` fatal
  (negative contract, unchanged).
- `COMMON_KNOWLEDGE` on a link-free synthesis claim → `D7` missing-declaration
  (unchanged backstop).

### 7. Config

`MEMEX_RESOLVER` — same pattern as `MEMEX_JUDGE`:

- `deterministic` (default) — zero runtime dependencies.
- `auto` — deterministic, then extractive on sub-threshold.
- `extractive:<model>` — extractive always.

### 8. Storage & schema

Persisted: a new `evidence` JSON column on the `node` table, one record per
SUPPORTED claim:

```json
[{"claim_index": 3, "parent_key": "P2", "span_text": "…", "confidence": 0.82, "resolver": "deterministic"}]
```

Rendered optionally (phase 2). Migration via a one-shot backfill, mirroring the
`backfill-synthesis` precedent.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Storage | Persisted (`evidence` JSON column) |
| Default resolver | `deterministic` (zero deps) |
| Gate threshold | `0.6` (min 2 content tokens) |
| Extractive model | span-extraction QA (DistilBERT/SQuAD-class) |

## Out of scope / risks

- **PDF extraction quality** is the separate root cause of the remaining D7
  misses on arXiv sources (tracked in `issue-slice-1-pdf-extraction.md`); this
  PRD does not change the extractor.
- The extractive resolver is an optional upgrade; shipping it is a separate
  slice (model choice, weights, runtime, latency budget).
- `evidence_hint` may still be paraphrased — that is now harmless (it is a
  locator), but a severely wrong hint can misalign the extractive resolver;
  the coverage gate is the backstop.
