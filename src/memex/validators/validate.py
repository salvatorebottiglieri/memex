"""Adversarial validation: checks if a derivation genuinely re-elaborates its parent.

The validation agent is a *separate* agent from the one that produced the
derivation (impartial judge). The prompt asks it to be critical.
"""

from memex.agent import Agent
from memex.schemas import DerivationResult, DocumentRef
from memex.derivers.demo import DemoAgent

_VERIFY_QUALITY_PROMPT = """\
You are a strict quality-control assistant for a knowledge-graph system.
A "derivation" node in this system summarises source content and adds new insight.
Your job is to verify the derivation genuinely re-elaborates the parent content:
it should not just copy or lightly paraphrase.

The derivation's synthesis statements are listed below. Evaluate each one:

1. Does the statement go beyond what the parent content explicitly says?
   (Yes = good; it's real synthesis)
2. Is the statement supported by the reasoning in the derivation body?
   (Yes = good; it's not a hallucination)
3. Is the statement specific enough to be useful?
   (Yes = good; "this is important" is too vague)

Submit your verdict by calling the submit_validation tool with the fields
passes (boolean) and reason (optional string). If the submit_validation tool
is unavailable, return ONLY a JSON object with key "passes" (boolean) and
optionally a "reason" string.

Parent content:
{parent_content}

Derivation:
{derivation_prose}

Synthesis statements:
{statements}
"""


def validate_derivation(
    agent: Agent,
    parent_content: str | None,
    derivation: DerivationResult,
    *,
    reference: DocumentRef | None = None,
) -> tuple[bool, str | None]:
    """Adversarial validation: check if derivation genuinely re-elaborates parent.

    Reader validators receive ``reference`` instead of inlined parent content
    and read the parent document themselves.

    Returns (bool, str | None):
      (True, None) — clean pass
      (False, None) — clean fail (validator rejected)
      (True, "warning message") — pass but validator had issues
    """
    # DemoAgent / mock: no real validation, always pass
    if isinstance(agent, DemoAgent):
        return True, None

    # Agents with call_llm: adversarial LLM call
    call = getattr(agent, "call_llm", None)
    if call is None:
        return True, None  # Unknown agent type, skip validation

    allow_read = bool(reference is not None and getattr(agent, "can_read_files", False))
    if allow_read:
        docs = reference if isinstance(reference, list) else [reference]
        doc_lines = "\n".join(
            f"- path: {d.content_path}\n  size_bytes: {d.size_bytes}"
            for d in docs
        )
        parent_block = (
            "Parent content is available at the paths below — read every file "
            "yourself with the read tool:\n" + doc_lines
        )
    else:
        parent_block = parent_content or "(no parent content)"
    statements = "\n".join(f"- {s}" for s in derivation.synthesis_statements)
    try:
        prompt = _VERIFY_QUALITY_PROMPT.format(
            parent_content=parent_block,
            derivation_prose=derivation.prose,
            statements=statements,
        )
    except (KeyError, ValueError, AttributeError):
        return True, "Validator prompt formatting failed, validation skipped"
    try:
        try:
            raw = call(prompt, allow_read=allow_read)
        except TypeError:
            # Legacy validator callables without the allow_read keyword.
            raw = call(prompt)
    except Exception:
        return True, "Validator LLM call failed, validation skipped"

    # Structured path: agents with host tools submit the verdict via the
    # submit_validation tool — no text parsing. Fall back to JSON-in-text.
    payload = None
    getter = getattr(agent, "last_tool_payload", None)
    if callable(getter):
        payload = getter("submit_validation")
    if isinstance(payload, dict) and isinstance(payload.get("passes"), bool):
        return payload["passes"], None

    import json as _json

    try:
        data = _json.loads(raw)
    except (ValueError, TypeError, _json.JSONDecodeError):
        return True, "Validator response parse failed, validation skipped"

    if not isinstance(data, dict) or "passes" not in data:
        return True, "Validator response missing 'passes' field, validation skipped"

    return bool(data["passes"]), None
