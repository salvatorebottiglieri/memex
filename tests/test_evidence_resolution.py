"""Unit tests for deterministic evidence resolution (validators/evidence.py).

Pins the System Invariants at the resolver/gate level:

- I2 (fail-closed): a SUPPORTED claim with fewer than MIN_CONTENT_TOKENS (2)
  checkable content tokens is ungrounded, never auto-verified on that claim.
- I4 (verbatim by construction): every resolved span_text is a verbatim
  substring of the NORMALIZED source (the resolver selects, never generates).
- I6 (anti-fabrication / window tightness): a fabricated or paraphrased
  candidate whose content does not co-occur in a tight passage of the
  source cannot pass the gate.
- I7 (deterministic): identical inputs produce identical Evidence.
"""
from __future__ import annotations

import pytest

from memex.validators.evidence import (
    GROUNDING_THRESHOLD,
    MIN_CONTENT_TOKENS,
    DeterministicResolver,
    Evidence,
    content_tokens,
    grounding_gate,
    normalize_surface,
    resolve_evidence,
    resolver_mode,
)

RESOLVER = DeterministicResolver()


# ── normalize_surface (the shared comparison surface) ────────────────

class TestNormalizeSurface:
    def test_nul_bytes_stripped(self):
        assert "\x00" not in normalize_surface("a\x00b\x00c")
        assert normalize_surface("a\x00b\x00c") == "abc"

    def test_html_entities_decoded(self):
        assert normalize_surface("it&#x27;s") == "it's"
        assert normalize_surface("a &amp; b") == "a & b"

    def test_nfkc_fullwidth_and_math_alpha_fold(self):
        # Fullwidth digits fold to ASCII; math italic 'w' folds to 'w'.
        assert normalize_surface("\uff14\uff12") == "42"
        assert normalize_surface("weight \U0001d464") == "weight w"

    def test_lookalike_quotes_and_dashes_fold(self):
        assert normalize_surface("the model\u2019s \u2018quote\u2019") == (
            "the model's 'quote'"
        )
        assert normalize_surface("a\u2014b") == "a-b"
        assert normalize_surface("\u201cquoted\u201d") == '"quoted"'

    def test_ascii_identity(self):
        text = "Plain ASCII prose, with numbers 42."
        assert normalize_surface(text) == text


# ── content_tokens ───────────────────────────────────────────────────

class TestContentTokens:
    def test_stopwords_and_short_words_excluded(self):
        assert content_tokens("This claim is fine.") == ["claim", "fine"]

    def test_numbers_always_count(self):
        assert "2024" in content_tokens("In 2024 the release shipped.")
        assert "42" in content_tokens("The answer is 42.")

    def test_len_two_words_never_content(self):
        assert "the" not in content_tokens("The real fact is stated.")
        # "ox" is a 2-letter word: dropped even though it is a noun.
        assert content_tokens("An ox is in it.") == []

    def test_wikilink_markup_dropped(self):
        tokens = content_tokens("Alpha lives in [[p-a|Parent A]] today.")
        assert "p-a" not in tokens
        assert "parent" not in tokens
        assert "alpha" in tokens and "today" in tokens

    def test_case_folded_and_deduped(self):
        assert content_tokens("Database DATABASE database") == ["database"]

    def test_curly_apostrophe_folds_for_tokenization(self):
        # "model's" tokenizes to model (+ s, dropped); the claim and the
        # source must agree on the apostrophe surface.
        assert content_tokens("the model\u2019s recall") == ["model", "recall"]


# ── resolver basics ──────────────────────────────────────────────────

class TestResolver:
    def test_verbatim_sentence_found_with_full_coverage(self):
        source = (
            "The database exports the full ledger each evening. "
            "Other unrelated content fills this passage out nicely."
        )
        ev = RESOLVER.resolve("The database exports the full ledger.", source)
        assert ev is not None
        assert ev.confidence == 1.0
        assert ev.resolver == "deterministic"
        assert "database exports the full ledger" in ev.span_text

    def test_no_overlap_returns_none(self):
        ev = RESOLVER.resolve(
            "Quantum entanglement teleports the payload.",
            "The database exports the full ledger each evening.",
        )
        assert ev is None

    def test_empty_candidate_or_source_returns_none(self):
        assert RESOLVER.resolve("", "some source content here.") is None
        assert RESOLVER.resolve("some claim text", "") is None
        assert RESOLVER.resolve("  ", "some source content here.") is None

    def test_spacing_and_entity_artifacts_tolerated(self):
        # Extraction spacing artifact around a colon + HTML-entity artifact.
        source = "context rot &#58; as the number of tokens increases. "
        assert RESOLVER.resolve("context rot: as the number of tokens", source)
        # NUL artifact inside the source.
        assert RESOLVER.resolve(
            "token counts degrade",
            "token \x00 counts \x00 degrade with noisy sources.",
        )

    def test_confidence_equals_candidate_token_coverage(self):
        # Two of the candidate's four content tokens co-occur in one tight
        # passage (coverage 0.5); the third sits beyond any window from it
        # and the fourth is absent.
        sentences = [
            "Unrelated filler sentence number %d." % i for i in range(30)
        ]
        sentences[0] = "Alpha is the first letter."
        sentences[1] = "Beta follows immediately."
        sentences[15] = "Gamma appears far away."
        source = " ".join(sentences)
        ev = RESOLVER.resolve("Alpha Beta Gamma Delta", source)
        assert ev is not None
        assert ev.confidence == 0.5

    def test_single_sentence_source_whole_passage(self):
        source = "This is a longer article body that exceeds the threshold."
        ev = RESOLVER.resolve("article body exceeds the threshold", source)
        assert ev is not None
        assert ev.confidence == 1.0


class TestI4VerbatimByConstruction:
    """I4: the resolver selects spans from the source; span_text is always a
    verbatim substring of the NORMALIZED source."""

    @pytest.mark.parametrize(
        "candidate,source",
        [
            (
                "the database exports the full ledger",
                "The database exports the full ledger each evening. "
                "Then the night shift takes over the export run quietly.",
            ),
            (
                "Alpha is the first letter",
                "Alpha is the first letter. Beta follows it. Gamma closes.",
            ),
            (
                "unicode model\u2019s weight",
                "The unicode model\u2019s weight \U0001d464 folds cleanly. "
                "Nothing else in this passage matches.",
            ),
            (
                "entity decode",
                "HTML entities like &#x27; and &amp; decode to plain "
                "characters before any matching happens.",
            ),
        ],
    )
    def test_span_is_substring_of_normalized_source(self, candidate, source):
        norm = normalize_surface(source)
        ev = RESOLVER.resolve(candidate, source)
        assert ev is not None
        assert ev.span_text in norm, (
            "span_text must be verbatim from the normalized source"
        )
        assert ev.start is not None and ev.end is not None
        assert norm[ev.start : ev.end] == ev.span_text
        assert ev.start >= 0 and ev.end <= len(norm)


class TestI2FailClosed:
    """I2: below MIN_CONTENT_TOKENS a claim is ungrounded even when the
    evidence would otherwise cover it."""

    def test_one_token_claim_never_grounds(self):
        claim = "Fine."
        assert len(content_tokens(claim)) == 1
        evidence = Evidence(
            span_text="Everything is fine and fully supported.",
            confidence=1.0,
            resolver="deterministic",
        )
        # Every content token (the one there is) is present — still ungrounded.
        assert grounding_gate(claim, evidence) is False

    def test_zero_token_claim_never_grounds(self):
        evidence = Evidence(span_text="content", confidence=1.0, resolver="deterministic")
        assert grounding_gate("Is it?", evidence) is False

    def test_two_content_tokens_ground_at_threshold(self):
        claim = "Ledger export"
        assert len(content_tokens(claim)) == MIN_CONTENT_TOKENS
        evidence = Evidence(
            span_text="The ledger export runs every evening.",
            confidence=1.0,
            resolver="deterministic",
        )
        assert grounding_gate(claim, evidence) is True


class TestGroundingGate:
    def test_coverage_below_threshold_fails(self):
        claim = "Database exports full ledger nightly"
        tokens = content_tokens(claim)
        assert len(tokens) >= MIN_CONTENT_TOKENS
        evidence = Evidence(
            span_text="the database export ledger",  # 2 of 4 tokens
            confidence=1.0,
            resolver="deterministic",
        )
        assert grounding_gate(claim, evidence) is False

    def test_coverage_at_threshold_passes(self):
        # 3 of 5 content tokens present == exactly 0.6.
        evidence = Evidence(
            span_text="database exports ledger present here",
            confidence=1.0,
            resolver="deterministic",
        )
        assert grounding_gate(
            "Database exports the full ledger completely", evidence
        ) is True

    def test_full_coverage_passes(self):
        evidence = Evidence(
            span_text="The database exports the full ledger each evening.",
            confidence=1.0,
            resolver="deterministic",
        )
        assert grounding_gate(
            "The database exports the full ledger", evidence
        ) is True

    def test_gate_does_not_depend_on_resolver_confidence(self):
        # The gate measures claim tokens vs span_text; the resolver's own
        # confidence score is informational (cascade input), not the gate.
        evidence = Evidence(
            span_text="database exports the full ledger",
            confidence=0.0,
            resolver="deterministic",
        )
        assert grounding_gate("Database exports ledger", evidence) is True

    def test_threshold_constants_locked(self):
        assert GROUNDING_THRESHOLD == 0.6
        assert MIN_CONTENT_TOKENS == 2


class TestI6AntiFabricationAndTightness:
    """I6: a fabricated or paraphrased hint whose content does not co-occur
    in a tight passage cannot ground a claim."""

    def test_paraphrase_without_co_occurrence_fails_gate(self):
        # The source says it one way; the candidate paraphrases it. Only
        # shared tokens ("database"/"ledger") land in one tight passage —
        # sub-threshold coverage, so the gate refuses.
        source = (
            "The database exports the full ledger each evening. "
            "Export jobs run unattended overnight every day."
        )
        claim = "The database emits the complete ledger automatically"
        ev = RESOLVER.resolve(claim, source)
        assert ev is not None
        assert ev.confidence < GROUNDING_THRESHOLD
        assert grounding_gate(claim, ev) is False

    def test_scattered_tokens_across_distant_sentences_do_not_ground(self):
        # Each candidate token sits in its own sentence, further apart than
        # any tight window can span — no passage co-locates enough of them.
        sentences = [
            "Unrelated filler sentence number %d." % i for i in range(30)
        ]
        sentences[0] = "Alpha here alone."
        sentences[12] = "Beta sits far away."
        sentences[24] = "Gamma too is isolated."
        source = " ".join(sentences)
        claim = "Alpha Beta Gamma"
        ev = RESOLVER.resolve(claim, source)
        assert ev is not None
        assert grounding_gate(claim, ev) is False

    def test_fabricated_candidate_absent_returns_none(self):
        source = (
            "The database exports the full ledger each evening. "
            "Nothing here resembles that invented claim at all."
        )
        ev = RESOLVER.resolve(
            "this fabricated quote appears nowhere in the source", source
        )
        assert ev is None

    def test_tight_passage_preferred_over_loose_document(self):
        # Both tokens co-occur tightly in sentence two; the resolver must
        # return the tight sentence, not a document-wide passage.
        source = (
            "Unrelated filler fills this first sentence completely. "
            "The ledger export succeeded overnight. "
            "Unrelated filler fills this third sentence completely. "
            "Even more unrelated filler lives in the fourth sentence here."
        )
        ev = RESOLVER.resolve("ledger export succeeded", source)
        assert ev is not None
        assert "ledger export succeeded" in ev.span_text
        assert len(ev.span_text) < len(source)


class TestI7Determinism:
    """I7: identical inputs give identical span + confidence."""

    def test_repeated_calls_identical(self):
        source = (
            "The database exports the full ledger each evening. "
            "Other content fills this source out. "
            "A third sentence adds yet more unrelated prose here."
        )
        candidate = "database exports the full ledger"
        first = resolve_evidence(candidate, source)
        for _ in range(10):
            again = resolve_evidence(candidate, source)
            assert again == first
            assert again.span_text == first.span_text
            assert again.confidence == first.confidence
            assert (again.start, again.end) == (first.start, first.end)


class TestResolverMode:
    """MEMEX_RESOLVER: deterministic default; auto/extractive degrade loudly."""

    def test_default_is_deterministic(self, monkeypatch):
        monkeypatch.delenv("MEMEX_RESOLVER", raising=False)
        assert resolver_mode() == "deterministic"

    def test_explicit_deterministic(self, monkeypatch):
        monkeypatch.setenv("MEMEX_RESOLVER", "deterministic")
        assert resolver_mode() == "deterministic"

    def test_auto_warns_and_degrades(self, monkeypatch, capsys):
        monkeypatch.setenv("MEMEX_RESOLVER", "auto")
        assert resolver_mode() == "deterministic"
        assert "MEMEX_RESOLVER=auto" in capsys.readouterr().err

    def test_extractive_model_warns_and_degrades(self, monkeypatch, capsys):
        monkeypatch.setenv("MEMEX_RESOLVER", "extractive:span-qa")
        assert resolver_mode() == "deterministic"
        assert "extractive" in capsys.readouterr().err

    def test_unknown_mode_warns_and_degrades(self, monkeypatch, capsys):
        monkeypatch.setenv("MEMEX_RESOLVER", "banana")
        assert resolver_mode() == "deterministic"
        assert "banana" in capsys.readouterr().err

    def test_seam_resolves_deterministically_under_auto(self, monkeypatch, capsys):
        # resolve_evidence dispatches through the seam; under 'auto' (not
        # shipped) it degrades loudly and still resolves deterministically.
        monkeypatch.setenv("MEMEX_RESOLVER", "auto")
        source = "The database exports the full ledger each evening."
        ev = resolve_evidence("database exports the ledger", source)
        assert ev is not None
        assert ev.resolver == "deterministic"
        assert "MEMEX_RESOLVER=auto" in capsys.readouterr().err
