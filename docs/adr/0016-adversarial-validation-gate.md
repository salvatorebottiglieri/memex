# ADR-0016: Adversarial validation gate for derivation quality

A derivation node (notes-tier or synthesis-tier) can be created without genuinely
re-elaborating its parent content. The deterministic checks (ADR-0011) verify structure
(`> Synthesis:` marker, no dangling references) — they cannot verify semantic quality.
The trust-state cascade (ADR-0014) propagates parent regression but does not evaluate the
derivation itself. `DemoAgent` produces boilerplate ("This article discusses the topic at
hand") that passes every deterministic check and gets marked `auto-verified`; even
production LLMs can produce shallow derivations. There is no gate that asks: *does this
derivation actually say something specific about its source?*

## Decision

Add an adversarial validation gate between derivation production and persistence. After
the derivation agent produces a `DerivationResult` but before any file or DB write, a
**separate validator agent** evaluates whether the derivation meaningfully re-elaborates
its parent content. If validation fails, the derivation is rejected and never stored.

### Validator agent

- Loaded from `MEMEX_VALIDATOR` (same `module:Class` convention as `MEMEX_AGENT`).
  Unset → validation skipped entirely (backwards compatible).
- Typically the same model type as the deriver, but with a **different, adversarial
  system prompt** that asks it to be critical and find flaws.
- Reader agents (`can_read_files`) may receive the parent `DocumentRef` instead of
  inlined content, matching the deriver seam.
- Dispatch in `validate_derivation(agent, parent_content, derivation) -> (passes, warning)`:
  - `DemoAgent` validator → always passes (test mocks unaffected).
  - Agent exposing `call_llm` → adversarial LLM call, parses `{"passes": bool}`.
  - Unknown agent type → passes (skip validation).

### Failure semantics

- **Rejected derivation** → `status: "quality_failed"` in the JSON result, no node and no
  edge written, no automatic retry — escalation to the user, who decides whether to
  re-derive or skip the source.
- **Validator unavailable** (LLM timeout, parse error, missing `passes` field) → derivation
  passes with a `validator_warning` surfaced in the result and stderr. Infrastructure
  failure never blocks persistence.
- Batch mode (`derive --all`) continues processing remaining items when one derivation
  fails validation.

### Scope

The gate applies to both `memex derive` and `memex synthesize`. It is pre-persistence:
**no schema changes**, no new columns, no new env vars.

## Consequences

- **Positive**: derivations that do not genuinely re-elaborate their source never enter
  the graph; the graph contains only real re-elaborations.
- **Positive**: `quality_failed` is a first-class JSON status agents can handle uniformly.
- **Positive**: backwards compatible — unset `MEMEX_VALIDATOR` leaves existing workflows
  untouched.
- **Negative**: one extra LLM call per derivation when the validator is configured. The
  cost of a rejected derivation (deriver + validator call) is sunk by design — storing a
  bad node costs more than the validation call.
- **Negative**: the gate is binary (pass/fail); no numeric quality score, no calibration.
- **Risk**: the validator's judgment is an LLM's — a weak validator adds cost without
  protection. The adversarial prompt is the control; the `DemoAgent`/unknown-agent pass
  path is deliberate so test suites and custom agents are never blocked.
