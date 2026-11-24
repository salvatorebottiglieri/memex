"""The derivation prompt rules shared by every deriver.

Each deriver appends its own submission rule (plain JSON envelope, or the
``submit_derivation`` tool call for wires that expose host tools) and, in
reader mode, its own instructions for reading the documents; every other rule
lives here, so the copies cannot drift apart again.

Reader mode is what ``memex derive`` actually runs for the reader agents
(``can_read_files`` = true), so the wording rule has to reach
``_DERIVE_READER_SYSTEM_PROMPT`` too — not just the inline prompt.

Rule 2 is deliberately the strictest of the variants the derivers used to
carry, and the wording rule is operational rather than stylistic: the D7 gate
grounds the tokens of a claim against the source, so a paraphrase in different
word forms reads exactly like a fabrication, and a meta-claim ABOUT a source
("P1 presents …") shares no tokens with it at all.

The inline link in rule 4 names the link *key* because that is the wire
format: ``services/synthesize.py`` inlines ``# Sources`` as ``- [[P1|alias]]``
and resolves ``P1`` to the parent's filename afterwards, while reader mode
lists ``link_key: P1`` per document — hence the mode-neutral "listed for that
parent's document".
"""


DERIVE_HEADER = (
    "You are a research analysis assistant. Given a user's source material, produce a "
    "structured derivation note following these rules:\n"
)


def derive_rules(first: int = 1) -> str:
    """The rules every derivation prompt carries, numbered from ``first``.

    Reader prompts put three reading rules in front, so they number these from
    4; the cross-references inside rule 2 move with them.
    """
    heading, body, synthesis, fidelity = range(first, first + 4)
    return (
        f"{heading}. Start with a single top-level heading (#) carrying the note's title.\n"
        f"{body}. Write body prose that summarises the source. Start DIRECTLY with "
        "the first source-derived fact. NEVER write a preamble or intro "
        "(\"This note …\", \"The following …\", \"We summarize …\", \"This "
        "document covers …\"), NEVER a statement about the note's own "
        "structure (\"All body statements …\", \"Cross-source comparisons appear "
        "in the Synthesis section\"), and NEVER a meta-claim ABOUT a source "
        "(\"P1 presents three techniques\", \"source X argues\") — restate the "
        "fact itself instead. Every body sentence restates a fact FROM a "
        f"source (each linked per rule {fidelity}); any statement that goes beyond the "
        "source — a cross-source comparison, a generalization, a judgment, or "
        f"an inference — MUST go in the ## Synthesis section as a '> Synthesis:' "
        f"bullet (rule {synthesis}), never in the body prose. Reuse the source's own "
        "words: every body sentence MUST be a quote or near-quote of the "
        "sentence it restates, NEVER a paraphrase, with no commentary or "
        "connective framing of your own added; anything you express in your "
        "own words — an inference, a judgment, a comparison, or a commentary "
        f"on an example — MUST be a '> Synthesis:' bullet instead.\n"
        f"{synthesis}. End with a ## Synthesis section whose body is one or more bullet points, "
        "each of the form \"> Synthesis: <inference>\". There MUST be at least one "
        "such statement. The exact prefix '> Synthesis:' is required.\n"
        f"{fidelity}. Factual fidelity: statistics or specific numbers absent from the source "
        "material MUST be omitted or marked as a synthesis statement — never invented, "
        "rounded, or approximated from memory. In a synthesis over multiple sources, "
        "every fact taken from a source MUST be followed by an inline link "
        "[[link_key|alias]] naming the parent it comes from (the exact link key listed "
        "for that parent's document, e.g. [[P1|alias]]); a single-source note needs no links.\n"
    )
