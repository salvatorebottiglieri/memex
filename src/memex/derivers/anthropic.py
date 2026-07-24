"""Anthropic-backed implementations for the Agent seam."""

import json as _json

from memex.agent import Agent
from memex.schemas import DerivationResult, ReviewProposal
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


class AnthropicAgent(Agent):
    """Real Anthropic-backed agent using structured JSON output."""

    def derive(self, content: str) -> DerivationResult:
        import anthropic

        client = anthropic.Anthropic()

        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=_DERIVE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _DERIVE_USER_TEMPLATE.format(content=content),
                }
            ],
        )

        raw = message.content[0].text
        prose, statements = parse_derive_response(raw)
        return DerivationResult(prose=prose, synthesis_statements=statements)

    def review(
        self,
        target_content: str,
        asserting_content: str,
        edge_payload: dict,
    ) -> ReviewProposal:
        import anthropic

        client = anthropic.Anthropic()

        system_prompt = (
            "You are a review assistant for a knowledge-graph system. Given two pieces of content "
            "(the 'target' node and the 'asserting' edge or node) and metadata about the "
            "potentially-changing claim, determine which nodes materially depend on the contested claim.\n\n"
            "Return a JSON object with these fields:\n"
            '  "affected_node_ids": array of strings — IDs of nodes that materially depend on the contested claim\n'
            '  "damage_boundary_node_id": string or null — the deepest affected node, or null if none\n'
            '  "rationale_md": string — markdown explaining your reasoning\n'
            '  "confidence": string — one of "high", "medium", or "low"\n'
        )

        edge_context = ""
        if edge_payload:
            edge_context = f"\n\nEdge metadata:\n{_json.dumps(edge_payload, indent=2)}"

        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Review the impact of changing this claim.\n\n"
                        f"Target content (the node whose claim is contested):\n{target_content}\n\n"
                        f"Asserting content (the new evidence or edge):\n{asserting_content}"
                        f"{edge_context}"
                    ),
                }
            ],
        )

        raw = message.content[0].text
        try:
            data = _json.loads(raw)
            affected_node_ids = data.get("affected_node_ids", [])
            damage_boundary_node_id = data.get("damage_boundary_node_id")
            rationale_md = data.get("rationale_md", raw)
            confidence = data.get("confidence", "low")
        except (_json.JSONDecodeError, AttributeError, KeyError):
            affected_node_ids = []
            damage_boundary_node_id = None
            rationale_md = raw
            confidence = "low"

        return ReviewProposal(
            affected_node_ids=affected_node_ids,
            damage_boundary_node_id=damage_boundary_node_id,
            rationale_md=rationale_md,
            confidence=confidence,
        )

    def generate_title(self, content: str, url: str) -> str | None:
        """Infer a human-readable title via Anthropic LLM. Returns None on failure."""
        import anthropic

        client = anthropic.Anthropic()
        system_prompt = (
            "You are a title generation assistant. Given source material, generate a "
            "concise, human-readable title (max 10 words). Return ONLY the title as a "
            "plain string, no quotes, no formatting."
        )
        try:
            message = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=60,
                system=system_prompt,
                messages=[
                    {
                        "role": "user",
                        "content": f"Generate a title for this content:\n\n{content[:2000]}",
                    }
                ],
            )
            return message.content[0].text.strip()
        except Exception:
            return None

    def extract_ideas(self, content: str) -> list[str]:
        """Extract 3-5 key ideas via Anthropic LLM."""
        import anthropic

        client = anthropic.Anthropic()
        system_prompt = (
            "You are an idea extraction assistant. Given source material, identify 3-5 "
            "key ideas or concepts. Return ONLY a JSON array of strings, each 5-15 words."
        )
        message = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=512,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": f"Extract the key ideas from this content:\n\n{content}",
                }
            ],
        )
        raw = message.content[0].text
        try:
            return _json.loads(raw)
        except (_json.JSONDecodeError, AttributeError, TypeError):
            return []
