"""Deterministic evidence resolution (reference-based grounding).

The V1 judge emits only *references* plus an optional locator
(``evidence_hint``); the system resolves the supporting evidence span from
the cited parent deterministically — an LLM never emits the evidence text
itself. This module owns that resolution:

- ``normalize_surface`` — the comparison surface D7 already matched on
  (NUL-strip + NFKC + look-alike folding + HTML-entity decode), lifted from
  the old ``validate._unicode_norm`` so the resolver compares on exactly
  the surface the extractor/judge pipeline produced.
- ``content_tokens`` — claim checkable content: numbers + non-stopword
  words (len >= 3), normalized; wikilink markup dropped.
- ``DeterministicResolver`` — sliding window over sentence-aligned
  passages of the normalized source; returns the tightest passage with the
  best candidate content-token coverage; ``confidence = coverage``; None
  when the best coverage is zero. The span is selected, never generated —
  verbatim by construction.
- ``grounding_gate`` — fail-closed: ``|content_tokens(claim)| >= 2`` AND
  ``>= 60%`` of them appear in the resolved span.
- ``resolver_mode`` / ``resolve_evidence`` — the ``MEMEX_RESOLVER`` seam
  (``deterministic`` default; ``auto`` / ``extractive:<model>`` warn loudly
  and degrade to deterministic; the extractive resolver is a separate
  slice, never stubbed).

The gate can falsify a SUPPORTED verdict (D7 fatal) but never overturns an
UNSUPPORTED verdict.
"""

from __future__ import annotations

import html
import json as _json
import os
import re
import sys as _sys
import unicodedata
from dataclasses import dataclass

from memex.rules import _WIKILINK_RE

# Locked defaults (docs/prd/evidence-resolution.md).
GROUNDING_THRESHOLD = 0.6
MIN_CONTENT_TOKENS = 2

# Largest contiguous sentence window the deterministic resolver considers.
# A locator is <= ~30 words and a claim a sentence or two; capping the
# window keeps the "tight passage" guarantee honest — tokens that only
# co-occur across the whole document must not count as a passage.
_MAX_WINDOW_SENTENCES = 8

# Sentence splitter: end punctuation followed by whitespace. Sentence
# spans (offsets into the source) keep their internal whitespace but not
# the inter-sentence run, so any windowed span is a verbatim substring.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

_WORD_RE = re.compile(r"[A-Za-z0-9]+")

# Look-alike graphemes folded to ASCII (NFKC alone leaves curly quotes
# untouched): a judge's hint echo and the extracted source must compare on
# the same surface even when one writes ' and the other '.
_LOOKALIKE_TRANSLATION = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u00ab": '"', "\u00bb": '"',
    "\u2032": "'", "\u2033": '"',
    "\u00b4": "'", "\u0060": "'",
    "\u2010": "-", "\u2011": "-", "\u2012": "-",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
})

# English function words: content tokens are the *checkable* words of a
# claim — the words a supporting passage must actually contain. Closed-
# class/function words carry no grounding signal, so they never count
# toward the fail-closed minimum or the coverage denominator.
_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be
    because been before being below between both but by can cannot could
    did do does doing down during each few for from further had has have
    having he her here hers herself him himself his how i if in into is it
    its itself me more most my myself no nor not of off on once only or
    other ought our ours ourselves out over own same she should so some
    such than that the their theirs them themselves then there these they
    this those through to too under until up very was we were what when
    where which while who whom why will with would you your yours yourself
    yourselves also onto upon us may might must shall across among around
    because become becomes became becoming been being
    """.split()
)


def _warn(message: str) -> None:
    _sys.stderr.write(_json.dumps({"validation_warning": message}) + "\n")


def normalize_surface(text: str) -> str:
    """NUL-strip + NFKC + look-alike folding + HTML-entity decode.

    The comparison surface D7 already matched on (lifted from
    ``validate._unicode_norm``): NUL bytes (PDF ToUnicode artifacts) are
    stripped first, then HTML entities are decoded and the result is
    unicode-normalized (math alphanumerics, superscripts, fullwidth) and
    folded so look-alike quotes/dashes compare equal.
    """
    return unicodedata.normalize(
        "NFKC", html.unescape((text or "").replace("\x00", ""))
    ).translate(_LOOKALIKE_TRANSLATION)


def content_tokens(text: str) -> list[str]:
    """Numbers + non-stopword words (len >= 3), normalized; wikilink markup
    dropped. Distinct tokens in first-seen order (a claim that repeats a
    word is still judged on the words it *contains*)."""
    surface = normalize_surface(text or "")
    # Wikilink markup ([[filename|alias]]) is a reference, not claim
    # content — the filename/alias never counts as checkable text.
    surface = _WIKILINK_RE.sub(" ", surface)
    tokens: list[str] = []
    seen: set[str] = set()
    for word in _WORD_RE.findall(surface.lower()):
        if word in seen:
            continue
        if word.isdigit():
            keep = True
        else:
            keep = len(word) >= 3 and word not in _STOPWORDS
        if keep:
            seen.add(word)
            tokens.append(word)
    return tokens


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of the sentence-aligned passages of *text*.

    Inter-sentence whitespace runs (the splitter's separator) are excluded
    from the spans, so concatenating any contiguous window of spans yields
    a verbatim substring of *text* (its internal whitespace preserved).
    """
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        if text[start:m.start()].strip():
            spans.append((start, m.start()))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


@dataclass(frozen=True)
class Evidence:
    """A resolved evidence span: verbatim text from the NORMALIZED source.

    ``confidence`` is the resolver's alignment score (0..1; == the
    candidate-token coverage for the deterministic resolver). ``resolver``
    names the engine that produced the span ("deterministic"; "extractive"
    later). ``start``/``end`` are char offsets into the normalized source
    (phase-2 audit surface).
    """

    span_text: str
    confidence: float
    resolver: str
    start: int | None = None
    end: int | None = None


class DeterministicResolver:
    """Zero-dependency resolver: sliding window + token coverage.

    Sentence-aligned windows over the normalized source; every window is
    scored by candidate content-token coverage; the tightest passage with
    the best coverage wins; ``confidence = coverage``; None when no window
    overlaps the candidate (best coverage 0). Tolerant of spacing/unicode/
    entity artifacts (everything compares on ``normalize_surface``), not
    tolerant of paraphrase (whole-token presence).
    """

    resolver = "deterministic"

    def resolve(self, candidate: str, source: str) -> Evidence | None:
        candidate_tokens = content_tokens(candidate or "")
        if not candidate_tokens:
            return None
        norm = normalize_surface(source or "")
        if not norm.strip():
            return None
        spans = _sentence_spans(norm)
        if not spans:
            return None
        # Per-sentence presence of each candidate token, as a bitmask
        # (bit k set -> sentence contains candidate_tokens[k]).
        token_index = {t: i for i, t in enumerate(candidate_tokens)}
        sentence_masks: list[int] = []
        for start, end in spans:
            mask = 0
            for token in content_tokens(norm[start:end]):
                idx = token_index.get(token)
                if idx is not None:
                    mask |= 1 << idx
            sentence_masks.append(mask)

        best: tuple[float, int, int, int, int] | None = None
        n = len(spans)
        max_size = min(n, _MAX_WINDOW_SENTENCES)
        # (coverage, span length) are the quality axes; ties keep the
        # leftmost window (deterministic).
        for size in range(1, max_size + 1):
            for i in range(n - size + 1):
                mask = 0
                for k in range(i, i + size):
                    mask |= sentence_masks[k]
                coverage = mask.bit_count() / len(candidate_tokens)
                if coverage == 0:
                    continue
                start = spans[i][0]
                end = spans[i + size - 1][1]
                span_len = end - start
                if best is None:
                    best = (coverage, span_len, i, start, end)
                else:
                    best_cov, best_len, *_ = best
                    if coverage > best_cov or (
                        coverage == best_cov and span_len < best_len
                    ):
                        best = (coverage, span_len, i, start, end)
        if best is None:
            return None
        coverage, _span_len, _i, start, end = best
        # Trim the window's outer whitespace (only possible at text edges;
        # sentence spans never begin/end mid-word).
        while start < end and norm[start].isspace():
            start += 1
        while end > start and norm[end - 1].isspace():
            end -= 1
        return Evidence(
            span_text=norm[start:end],
            confidence=coverage,
            resolver=self.resolver,
            start=start,
            end=end,
        )


def resolver_mode() -> str:
    """``MEMEX_RESOLVER`` mode: 'deterministic' (default) | 'auto' |
    'extractive:<model>'. ``auto`` and ``extractive:<model>`` warn loudly
    and degrade to deterministic — the extractive resolver is a separate
    slice and is never silently stubbed.
    """
    mode = os.environ.get("MEMEX_RESOLVER", "").strip().lower()
    if not mode or mode == "deterministic":
        return "deterministic"
    if mode in ("auto",) or mode.startswith("extractive:"):
        _warn(
            "MEMEX_RESOLVER=" + mode + " is not shipped yet (extractive "
            "resolver is a separate slice); degrading to deterministic"
        )
    else:
        _warn(
            f"MEMEX_RESOLVER={mode} is not a supported resolver mode; "
            "degrading to deterministic"
        )
    return "deterministic"


def resolve_evidence(candidate: str, source: str) -> Evidence | None:
    """PRD seam: resolve *candidate* inside *source*.

    ``resolver_mode()`` is consulted first so a non-default
    ``MEMEX_RESOLVER`` setting degrades loudly; the deterministic resolver
    is the only engine today (extractive runs only on sub-threshold once
    shipped, never silently stubbed).
    """
    resolver_mode()
    return DeterministicResolver().resolve(candidate, source)


def grounding_gate(claim: str, evidence: Evidence) -> bool:
    """Fail-closed grounding gate over a SUPPORTED claim.

    tokens = content_tokens(claim); grounded <=> |tokens| >= MIN_CONTENT_TOKENS
    AND the fraction of tokens present in the resolved span_text is >=
    GROUNDING_THRESHOLD. Below the minimum token count the claim has no
    checkable content -> ungrounded (never auto-verified on that claim).
    """
    tokens = content_tokens(claim or "")
    if len(tokens) < MIN_CONTENT_TOKENS:
        return False
    span_tokens = set(content_tokens(evidence.span_text or ""))
    present = sum(1 for t in tokens if t in span_tokens)
    return present / len(tokens) >= GROUNDING_THRESHOLD
