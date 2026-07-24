"""Derivers backed by Pi / OMP CLI tools."""

from memex.agent import Agent
from memex.schemas import DerivationResult
from memex.utils.parsing import parse_derive_response

_DERIVE_SYSTEM_PROMPT = (
    "You are a research analysis assistant. Given a user's source material, produce a "
    "structured derivation note following these rules:\n"
    "1. Start with a single top-level heading (#) carrying the note's title.\n"
    "2. Write body prose that summarises the source. Facts restated from the source "
    "are unadorned; any statement that goes beyond what the source says must be "
    "marked as a synthesis statement.\n"
    "3. End with a ## Synthesis section whose body is one or more bullet points, "
    "each of the form \"> Synthesis: <inference>\". There MUST be at least one "
    "such statement. The exact prefix '> Synthesis:' is required.\n"
    "4. Return your response as a JSON object with keys: 'prose' (the full markdown), "
    "'synthesis_statements' (list of strings, each without the '> Synthesis:' prefix)."
)

_DERIVE_USER_TEMPLATE = "# Source material\n\n{content}\n"


class PiAgent(Agent):
    """Agent powered by Pi (``@earendil-works/pi-coding-agent``).

    Uses the ``pi`` CLI under the hood (``pi -p --mode json --no-session --no-tools``).
    The Pi SDK (TypeScript) at https://pi.dev/docs/latest/sdk provides the full agent
    runtime for JS/TS projects; this Python integration uses the CLI interface.

    Requires ``pi`` to be installed and available on PATH.
    Supports any provider/model configured in ``pi`` (e.g. Claude, GPT, Gemini, DeepSeek).
    """

    _cli_cmd = "pi"

    def call_llm(self, prompt: str) -> str:
        import json as _json
        import subprocess as _sp

        try:
            proc = _sp.run(
                [self._cli_cmd, "-p", "--mode", "json", "--no-session", "--no-tools"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{type(self).__name__} requires the '{self._cli_cmd}' CLI. "
                f"Install it from https://{self._cli_cmd}.dev"
            ) from None
        except _sp.TimeoutExpired:
            raise RuntimeError(f"{type(self).__name__} call timed out after 120s") from None

        if proc.returncode != 0:
            raise RuntimeError(f"{type(self).__name__} call failed: {proc.stderr.strip()}")

        # Parse JSON lines output — extract text from the last message_end
        last_text = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                msg = event.get("message", {})
                content = msg.get("content", [])
                for part in content:
                    if part.get("type") == "text":
                        last_text = part.get("text", "")
        return last_text

    def derive(self, content: str) -> DerivationResult:
        prompt = _DERIVE_SYSTEM_PROMPT + "\n\n" + _DERIVE_USER_TEMPLATE.format(content=content)
        raw = self.call_llm(prompt)
        prose, statements = parse_derive_response(raw)
        return DerivationResult(prose=prose, synthesis_statements=statements)


class OMPAgent(PiAgent):
    """Agent powered by OMP (Oh My Pi — ``@nicedoc/oh-my-pi``).

    Uses the ``omp`` CLI under the hood (same interface as Pi).

    Requires ``omp`` to be installed and available on PATH.
    Supports any provider/model configured in ``omp`` (e.g. Claude, GPT, Gemini, DeepSeek).

    Usage: ``MEMEX_AGENT=memex.agent:OMPAgent``
    """

    _cli_cmd = "omp"

    def call_llm(self, prompt: str) -> str:
        import json as _json
        import subprocess as _sp

        try:
            proc = _sp.run(
                [self._cli_cmd, "-p", "--mode", "json", "--no-session", "--no-tools", prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{type(self).__name__} requires the '{self._cli_cmd}' CLI. "
                f"Install it from https://ohmy-pi.dev"
            ) from None
        except _sp.TimeoutExpired:
            raise RuntimeError(f"{type(self).__name__} call timed out after 120s") from None

        if proc.returncode != 0:
            raise RuntimeError(f"{type(self).__name__} call failed: {proc.stderr.strip()}")
        # Parse JSON lines output — extract text from the last message_end
        last_text = ""
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if event.get("type") == "message_end":
                msg = event.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            last_text = part.get("text", "")
        return last_text
