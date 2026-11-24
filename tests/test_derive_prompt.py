"""The derivation rules every deriver shares (memex.derivers.prompts).

The wording rule is load-bearing, not stylistic: the D7 gate grounds claim
tokens against the source, so a paraphrase in different word forms reads
exactly like a fabrication. A silent removal must fail here.
"""

from memex.derivers import anthropic, claude_code, pi
from memex.derivers.prompts import DERIVE_RULES


def test_derive_rules_ask_for_the_sources_own_words():
    for phrase in (
        "Reuse the source's own words",
        "a quote or near-quote of the sentence it restates",
        "NEVER a paraphrase",
        "no commentary or connective framing of your own",
        "a commentary on an example",
        "MUST be a '> Synthesis:' bullet instead",
    ):
        assert phrase in DERIVE_RULES, phrase


def test_every_deriver_composes_the_shared_rules():
    for module in (anthropic, claude_code, pi):
        assert module._DERIVE_SYSTEM_PROMPT.startswith(DERIVE_RULES), module.__name__
