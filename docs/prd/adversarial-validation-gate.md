# Adversarial validation gate for derivation quality

## Problem Statement

A derivation node (notes-tier or synthesis-tier) can be created without genuinely re-elaborating its parent content. The `DemoAgent` produces boilerplate ("This article discusses the topic at hand") that passes all deterministic checks and gets marked `auto-verified`. Even production LLMs can produce shallow or generic derivations under certain conditions.

The existing quality controls are:
- **Deterministic checks** (ADR-0011): verify structure (`> Synthesis:` marker, no dangling references). They do not and cannot verify semantic quality.
- **Trust state cascade** (ADR-0014): propagates parent regression, but does not evaluate the derivation itself.

There is no gate that asks: "Does this derivation actually say something specific about its source?" Derivations that fail this question should not be stored.

## Solution

Add an adversarial validation gate between derivation production and persistence. After the derivation agent produces a `DerivationResult` but before any file or DB write, a **separate validator agent** (loaded from `MEMEX_VALIDATOR`) evaluates whether the derivation meaningfully re-elaborates its parent content. If validation fails, the derivation is rejected and never stored.

The validator agent:
- Is the **same model type** as the derivation agent (e.g. both use OMPAgent with Claude), but operates with a **different, adversarial system prompt** that asks it to be critical and find flaws.
- Is loaded via `MEMEX_VALIDATOR` env var (same `module:Class` convention as `MEMEX_AGENT`). If unset, validation is skipped entirely (backwards compatible).
- When the validator itself fails (LLM timeout, parse error), the derivation passes with a warning — never blocks on infrastructure failure.

The gate applies to both `memex derive` and `memex synthesize`.

## User Stories

1. As a user, I want derivations to be validated for quality before they enter the graph, so that my knowledge base contains only genuine re-elaborations of source material.

2. As a user, I want the validator to use a different prompt from the deriver (adversarial/critical), so that the evaluation is impartial and catches boilerplate.

3. As a user, I want the validator to be a configurable agent (via `MEMEX_VALIDATOR`), so that I can choose which model does the evaluation.

4. As a user, I want the default to be no validation (if `MEMEX_VALIDATOR` is unset), so that existing workflows are not broken by the new gate.

5. As a user, I want a rejected derivation to produce a clear `quality_failed` status instead of silently creating a weak node, so that I know what happened.

6. As a user, I want `memex derive --all` to continue processing remaining items when one derivation fails validation, so that a single weak derivation does not block the batch.

7. As a user, I want the `quality_failed` result to be surfaced in the same JSON output format as other per-item errors, so that my agent can handle it uniformly.

8. As a user, I want derivation failure from the quality gate to be escalated to me (no automatic retry), so that I can decide whether to re-derive with a different prompt or skip the source.

9. As a user, I want to know if the validator itself was unavailable (timeout, parse error), so that I understand why unvalidated derivations entered the graph.

10. As a user, I want a validator failure to produce a warning in the result output and stderr, so that it is visible without being blocking.

11. As a user, I want `memex synthesize` to also pass through the quality gate, so that synthesis-tier derivations are validated against their combined parents.

12. As a user, I want the test mocks (`DemoAgent`, `FakeAgent`) to always pass validation, so that the test suite is not disrupted.

## Implementation Decisions

### `validate_derivation(agent, parent_content, derivation) -> (bool, str | None)`

A standalone function in `src/memex/agent.py`. Takes a validator agent (any object), the parent content string, and the proposed `DerivationResult`.

**Return**: `(passes, warning)` where:
- `passes: bool` — `True` if derivation is acceptable, `False` if quality gate rejects it.
- `warning: str | None` — set to a human-readable warning when the validator itself failed (LLM call error, parse failure). `None` on clean pass or clean fail.

**Dispatch**:
- `DemoAgent` validator → `(True, None)` (test mock, always passes).
- Agent with `call_llm` method (PiAgent, OMPAgent) → calls LLM with adversarial prompt, parses JSON `{"passes": true|false}`.
- Unknown agent type → `(True, None)` (skip validation).

**Adversarial prompt** (`_VERIFY_QUALITY_PROMPT`):
```
You are an adversarial validator for a personal knowledge graph (memex).
Your job is to be CRITICAL: a derivation must genuinely re-elaborate its source.
If the derivation is generic boilerplate, you must reject it.

SOURCE: {parent_content}
DERIVATION: {derivation_prose}
SYNTHESIS STATEMENTS: {statements}

Does this derivation meaningfully re-elaborate the source? Be strict.
A PASSING derivation references specific concepts, claims, or data from the source.
A FAILING derivation uses generic phrases like "the article discusses",
"the author covers", "the topic at hand" — boilerplate applicable to ANY source.

Answer with exactly the JSON object (no other text):
{"passes": true} or {"passes": false}
```

**Error handling**:
- LLM call fails (timeout, network) → `(True, "Validator LLM call failed, validation skipped")`
- LLM returns non-JSON → `(True, "Validator response parse failed, validation skipped")`
- JSON parsed but `passes` key missing → `(True, "Validator response missing 'passes' field, validation skipped")`

### Gate location

In `_do_derive` and `_do_synthesize` (src/memex/cli.py), between the `agent.derive()` call and the first DB/file write:

```python
deriv = agent.derive(content)

validator_path = os.environ.get("MEMEX_VALIDATOR")
if validator_path:
    validator = load_agent(validator_path)
    passes, warning = validate_derivation(validator, content, deriv)
    if warning:
        result_accumulator["validator_warning"] = warning
    if not passes:
        return {"status": "quality_failed", "reason": "...", "l0_node_id": l0_id}
```

### Result shape on failure

**`memex derive <id>`**:
```json
{"status": "quality_failed", "reason": "Derivation does not meaningfully re-elaborate the source material.", "l0_node_id": "<uuid>"}
```

**`memex synthesize <id>...`**:
```json
{"status": "quality_failed", "reason": "Synthesis does not meaningfully re-elaborate the source material.", "parent_ids": ["<uuid>", ...]}
```

### Result shape with validator warning

A successful derivation where the validator was unavailable:
```json
{"status": "derived", "id": "...", "trust_state": "auto-verified", ..., "validator_warning": "Validator LLM call failed, validation skipped"}
```

### Stderr output during batch

When validator fails:
```
[3/10] derived  https://example.com/article
[4/10] quality_failed  https://example.com/other  [validator unavailable]
```

### Module changes

**`src/memex/agent.py`**:
- Add `_VERIFY_QUALITY_PROMPT` constant (adversarial prompt).
- Add `validate_derivation(agent, parent_content, derivation) -> (bool, str | None)` function.
- No change to `Agent`, `DemoAgent`, `PiAgent`, `OMPAgent` classes (function is standalone).

**`src/memex/cli.py`**:
- `_do_derive`: add validator loading and gate call after `agent.derive()`.
- `_do_synthesize`: same pattern.
- No changes to CLI command signatures or Click decorators.

**No schema changes** — the quality gate is pre-persistence. No new columns, no new tables.

**No new env vars** — reuses `MEMEX_VALIDATOR` (follows `MEMEX_AGENT` convention).

## Testing Decisions

**What makes a good test**: the test asserts on the observable CLI output (JSON status) and on the absence of DB/file artifacts when the gate rejects. It does not test the LLM's judgment — the validator agent is mocked.

**Modules tested**:

- `src/memex/agent.py` — `validate_derivation` unit tests:
  - DemoAgent validator → always returns `(True, None)`.
  - Mock agent with `call_llm` returning `{"passes": true}` → `(True, None)`.
  - Mock agent with `call_llm` returning `{"passes": false}` → `(False, None)`.
  - Mock agent with `call_llm` raising → `(True, "warning")`.
  - Mock agent with `call_llm` returning garbage → `(True, "warning")`.

- `src/memex/cli.py` — the gate integration:
  - `_do_derive` with `MEMEX_VALIDATOR` unset → derive proceeds normally.
  - `_do_derive` with `MEMEX_VALIDATOR` set to `FakeAgent` (returns True) → derive proceeds.
  - `_do_derive` with a validator that returns `(False, None)` → `quality_failed`, no node/edge created.

- `tests/smoke_test.py` — end-to-end:
  - Existing tests unchanged (no `MEMEX_VALIDATOR` → gate skipped).

- The `validate_derivation` function is tested in `tests/test_validate_derivation.py`. The `tests/fake_llm_client.py:FakeAgent` needs no changes (it already doesn't have `call_llm`, so `validate_derivation` will treat it as unknown and pass).

## Out of Scope

- **Retry on quality failure**: if the validator rejects, the derivation is discarded and the user is notified. No automatic re-derive with a different prompt.
- **Validator model calibration**: no ground-truth set, no confidence calibration for the validator.
- **Per-parent validation for synthesize**: the validator receives the combined content of all parents and the full derivation. No per-parent breakdown.
- **Quality scoring**: the gate is binary (pass/fail). No numeric quality score.
- **Database changes**: no new columns, tables, or migrations. The gate is pre-persistence.
- **Separate validator CLI**: no new commands. The validator is invoked transparently inside derive/synthesize when the env var is set.

## Further Notes

- The validator is called **once per derivation**. If the derivation is rejected, the LLM cost for both the derivation call and the validator call is sunk. This is by design: the cost of storing a bad node is higher than the cost of the validation call.
- The `validate_derivation` function will work with any agent class that exposes `call_llm`. Custom agents that do not follow the PiAgent/OMPAgent pattern will skip validation (seen as unknown type). This is documented in the function docstring.
- The `DemoAgent` as validator always passes. This means in test mode (`no MEMEX_VALIDATOR`), there is no validation, and in production (`MEMEX_VALIDATOR=memex.agent:OMPAgent`), the OMPAgent validates. Both modes are intentional.
- The warning propagation to stderr and result dict is modelled on the existing `failed` field propagation pattern in the ingestion path.
