"""Parse helpers for LLM derive responses."""

import json


def parse_derive_response(raw: str) -> tuple[str, list[str]]:
    """Parse an LLM response into (prose, synthesis_statements).

    Tries the JSON envelope first; on failure, falls back to treating the
    whole response as prose with an empty statement list.
    """
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            prose = data.get("prose", data.get("content", raw))
            statements = data.get("synthesis_statements", [])
            if isinstance(statements, list):
                return prose, statements
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return raw, []
