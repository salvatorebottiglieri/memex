# ADR-0018: Validation family — always-on LLM-judged criteria over the deterministic gate

Status: **Accepted** (supersedes ADR-0016).

## Context

ADR-0016's adversarial validation gate — a separate `MEMEX_VALIDATOR` agent,
a pre-persistence `validate_derivation()` → `(passes, warning)` dispatch, and
`status: "quality_failed"` on rejection — was removed. The deterministic
checks (ADR-0011) verify structure, not semantic quality: boilerplate
("This article discusses the topic at hand") passes D1–D6 and gets
`auto-verified`, and production LLMs can produce shallow or unfaithful
derivations. The replacement gate must be:

- **Always-on** — no opt-in env var that silently disables quality;
- **Post-persistence** — the node exists with its annotations; humans
  decide, nothing is destroyed;
- **Two-level** — an unfounded claim is fatal; a boilerplate synthesis is a
  quality defect with a different (human-reviewable) signal.

ADR-0011 explicitly rejected "make every check an LLM call … gating the
trust state on the same model that produced the derivation (no independent
signal)". The validation family **reverses that rejection, deliberately and
bounded**: the judge is the derivation agent by default (`MEMEX_JUDGE` can
point elsewhere), because an LLM is still the only check that can read a
claim against its source. The bound is D7: V1's evidence quotes are verified
*deterministically* — an LLM that hallucinates a claim can hallucinate the
supporting quote, so the quote is checked against the parent content by
code, not by the model.

## Decision

A validation **family** of small, orthogonal LLM-judged criteria runs after
node creation, on every derive and synthesize, as a dependency-ordered DAG:

```
V1 (grounding) ──> D7 (deterministic quote verification over V1's verdicts)
    │
    └──> V2 (re-elaboration quality; consumes V1's verdicts;
           SKIPPED when V1 produced fatal failures)
```

- **Always-on**: `MEMEX_VALIDATION=off` disables only the LLM criteria. The
  deterministic checks D1–D6 never opt out; D7 is vacuous without V1's
  verdicts. `MEMEX_VALIDATOR` is gone, as are `validate_derivation()` and
  `status: quality_failed`.
- **Judge**: the agent that produced the derivation (RPC process reuse;
  `--no-session` keeps each judge turn a stateless single turn), or the
  agent named by `MEMEX_JUDGE` when set. Reader judges (`can_read_files`)
  receive path references and read the parent files themselves.
- **Registry-driven DAG**: `VALIDATION_RULES` (rules.py) declares each
  criterion with `order`, `depends_on`, `skip_when_fatal` and
  `expects_full_verdicts` fields; `run_validations` executes the waves in
  order through one shared `_run_wave` helper. D7 is a deterministic stage
  keyed to the V1 wave. Adding a criterion is one new `ValidationRule`
  entry — no `run_validations` edit.
- **Two-level severity**: every gate failure carries a tag — fatal (D6, D7,
  V1-UNSUPPORTED; one is enough → draft) vs quality (V2 → draft). Both
  severities gate to `draft`; the tag is an **informational annotation** in
  `check_failures` guiding human review. There is no separate
  `quality_failed` state: status stays `derived`/`synthesized`, the node is
  stored, and draft nodes are human-promotable via the existing review flow.
- **Contract enforcement**: an UNSUPPORTED verdict must cite
  `source_examined` + `absence_explanation` — a judge omitting either
  produces a deterministic contract-violation failure (symmetric to D7's
  SUPPORTED-without-quote failure). V1 verdict shortfalls (fewer verdicts
  than claims, including an empty set) emit a warning. V2 `passes` is
  coerced bool-ish ('true', 1, 'yes', …) like V1 normalizes verdict strings.
- **Graceful degradation**: judge-call or verdict-parse failures degrade to
  pass-with-warning — infrastructure failure never blocks persistence.

## Consequences

- **Positive**: every derivation is checked for grounding and re-elaboration
  quality, with the LLM's evidence quotes verified deterministically.
- **Positive**: always-on means no silently-skipped quality gate.
- **Positive**: annotations (check_failures with severity tags) persist with
  the node for human review; nothing is destroyed pre-persistence.
- **Positive**: the DAG is declarative — adding a criterion does not touch
  the executor.
- **Negative**: 1–2 extra LLM turns per derivation (V1 always; V2 unless V1
  was fatal).
- **Negative**: the judge is the derivation agent by default — the
  ADR-0011 rejection of "same model that produced the derivation" is
  explicitly reversed (bounded by D7's deterministic quote verification).
