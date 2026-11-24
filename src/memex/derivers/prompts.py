"""The derivation prompt rules shared by every deriver.

Each deriver appends its own submission rule (plain JSON envelope, or the
``submit_derivation`` tool call for wires that expose host tools); every other
rule lives here, so the three copies cannot drift apart again.

Rule 2 is deliberately the strictest of the variants the derivers used to
carry, and the wording rule is operational rather than stylistic: the D7 gate
grounds the tokens of a claim against the source, so a paraphrase in different
word forms reads exactly like a fabrication, and a meta-claim ABOUT a source
("P1 presents …") shares no tokens with it at all.

The inline link in rule 4 names the link *key* because that is the wire
format: ``services/synthesize.py`` builds ``# Sources`` as ``- [[P1|alias]]``
for every inline agent and resolves ``P1`` to the parent's filename afterwards.
"""

DERIVE_RULES = (
    "You are a research analysis assistant. Given a user's source material, produce a "
    "structured derivation note following these rules:\n"
    "1. Start with a single top-level heading (#) carrying the note's title.\n"
    "2. Write body prose that summarises the source. Start DIRECTLY with "
    "the first source-derived fact. NEVER write a preamble or intro "
    "(\"This note …\", \"The following …\", \"We summarize …\"), NEVER a "
    "statement about the note's own structure, and NEVER a meta-claim "
    "ABOUT a source (\"P1 presents …\", \"source X argues\") — restate the "
    "fact itself instead. Every body sentence restates a fact FROM a "
    "source (each linked per rule 4); any statement that goes beyond the "
    "source — a cross-source comparison, a generalization, a judgment, or "
    "an inference — MUST go in the ## Synthesis section as a '> Synthesis:' "
    "bullet (rule 3), never in the body prose. Reuse the source's own "
    "words: every body sentence MUST be a quote or near-quote of the "
    "sentence it restates, NEVER a paraphrase, with no commentary or "
    "connective framing of your own added; anything you express in your "
    "own words — an inference, a judgment, a comparison, or a commentary "
    "on an example — MUST be a '> Synthesis:' bullet instead.\n"
    "3. End with a ## Synthesis section whose body is one or more bullet points, "
    "each of the form \"> Synthesis: <inference>\". There MUST be at least one "
    "such statement. The exact prefix '> Synthesis:' is required.\n"
    "4. Factual fidelity: statistics or specific numbers absent from the source "
    "material MUST be omitted or marked as a synthesis statement — never invented, "
    "rounded, or approximated from memory. In a synthesis over multiple sources, "
    "every fact taken from a source MUST be followed by an inline link "
    "[[link_key|alias]] naming the parent it comes from (use the exact link key the "
    "# Sources block lists for that parent, e.g. [[P1|alias]]); a single-source "
    "note needs no links.\n"
)
