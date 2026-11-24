"""The derivation rules every deriver shares (memex.derivers.prompts).

The wording rule is load-bearing, not stylistic: the D7 gate grounds claim
tokens against the source, so a paraphrase in different word forms reads
exactly like a fabrication. It has to reach every composition — inline *and*
reader mode, which is the path ``memex derive`` runs for the reader agents.
"""

import pytest

from memex.derivers import anthropic, claude_code, pi
from memex.derivers.prompts import derive_rules

WORDING_RULE_PHRASES = (
    "Reuse the source's own words",
    "a quote or near-quote of the sentence it restates",
    "NEVER a paraphrase",
    "no commentary or connective framing of your own",
    "a commentary on an example",
    "MUST be a '> Synthesis:' bullet instead",
)


def _composed_prompts() -> dict[str, str]:
    return {
        "anthropic inline": anthropic._DERIVE_SYSTEM_PROMPT,
        "claude_code inline": claude_code._DERIVE_SYSTEM_PROMPT,
        "claude_code reader": claude_code._DERIVE_READER_SYSTEM_PROMPT,
        "pi inline": pi._DERIVE_SYSTEM_PROMPT,
        "pi reader": pi._DERIVE_READER_SYSTEM_PROMPT,
    }


@pytest.mark.parametrize("name", sorted(_composed_prompts()))
def test_every_derivation_prompt_carries_the_shared_rules(name: str) -> None:
    prompt = _composed_prompts()[name]
    for phrase in (
        *WORDING_RULE_PHRASES,
        "the exact link key listed for that parent's document",
        "Start DIRECTLY with the first source-derived fact",
    ):
        assert phrase in prompt, (name, phrase)


def test_derive_rules_renumbers_with_the_cross_references() -> None:
    assert derive_rules().startswith("1. Start with")
    assert derive_rules(first=4).startswith("4. Start with")
    assert "(each linked per rule 4)" in derive_rules()
    assert "(each linked per rule 7)" in derive_rules(first=4)
