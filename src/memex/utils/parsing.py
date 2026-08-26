"""Parse helpers for LLM derive responses."""

import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)

# Upper bound on source content fed to the LLM. Extraction can produce
# multi-megabyte files (broken PDF text layers, HTML dumps) that blow past
# any model context window; the summary needs the head of the source, not
# a token overflow. Kept explicit so a truncated prompt is honest about it.
# Shared by the derive path and the validation DAG (parent content inlined
# into judge prompts is capped identically).
_MAX_PROMPT_CHARS = 120_000

_TRUNCATION_NOTE = (
    "\n\n[source content truncated — exceeds prompt limit; "
    "the remainder was not considered]"
)


def _cap_prompt_content(content: str, limit: int = _MAX_PROMPT_CHARS) -> str:
    """Strip NUL bytes (PDF ToUnicode artifacts) and cap prompt size.

    ``limit`` overrides the cap (default ``_MAX_PROMPT_CHARS``): the
    validation DAG passes each inlined parent a proportional slice of the
    aggregate budget when several large parents share the prompt.
    """
    content = content.replace("\x00", "")
    if len(content) <= limit:
        return content
    return content[:limit] + _TRUNCATION_NOTE


def parse_synthesis_statements(raw: str | None) -> list[str]:
    """Parse the node ``synthesis_statements`` column (JSON array of strings).

    Tolerates a null/empty column, invalid JSON, and non-list payloads —
    the column is written by an LLM pipeline that may store garbage; a
    malformed value means no statements, never a crash. Shared by the D3
    deterministic check and the validation DAG's node context, which parse
    the column with identical semantics.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(s) for s in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def parse_derive_response(raw: str) -> tuple[str, list[str]]:
    """Parse an LLM response into (prose, synthesis_statements).

    Tries the JSON envelope first (tolerating markdown code fences, which
    CLI-driven agents commonly wrap around their output); on failure, falls
    back to treating the whole response as prose and recovering synthesis
    statements by regex (Rule S5).
    """
    stripped = _FENCE_RE.sub("", raw.strip())
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            prose = data.get("prose", data.get("content", raw))
            statements = data.get("synthesis_statements", [])
            if isinstance(statements, list):
                return prose, statements
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    statements = re.findall(r">\s*Synthesis:\s*(.+)", raw)
    return raw, statements
