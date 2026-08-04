"""Parse helpers for LLM derive responses."""

import json
import re

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


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
