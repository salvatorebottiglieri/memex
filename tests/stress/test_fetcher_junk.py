"""Stress — the fetcher must never turn junk into node content.

Invariant J1: HttpFetcher output is real page text. Script and style bodies
(JS bundles, CSS) are never part of the extracted content.

Invariant J2: a page with no extractable text yields empty content — an
expected content absence (ADR-0013), not a FetchError; the extract CLI
stores nothing in that case.

Regression corpus: real junk found stored in the production vault
(issues #111, #112, plus the YouTube/Amazon CSS/JS cases): samples live in
tests/fixtures/junk/. The junk invariant was violated on the pre-campaign
fetcher (tag stripping kept script/style bodies) — fixed in the campaign.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from memex.fetchers.http import HttpFetcher

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "junk"

# (fixture file, junk marker that must never appear in extracted content)
JUNK_CASES = [
    ("google-slides-js.txt", "window['ppConfig']"),
    ("youtube-wiz-js.txt", "window.WIZ_global_data"),
    ("instagram-css.txt", "--fds-black"),
    ("amazon-css.txt", "--wp--preset--color"),
]


def _fetch(monkeypatch, html: str) -> str:
    return _fetch_result(monkeypatch, html).content


def _fetch_result(monkeypatch, html: str):
    monkeypatch.setattr(
        "memex.fetchers.http.download_bytes",
        lambda url: html.encode("utf-8"),
    )
    return HttpFetcher().fetch("https://example.com/page")


def _wrap(junk: str, *, body: str = "<p>Actual readable text.</p>") -> str:
    return (
        "<html><head><title>Real page title</title></head>"
        f"<body><script>{junk}</script>{body}</body></html>"
    )


class TestJunkNeverBecomesContent:
    @pytest.mark.parametrize("fixture,marker", JUNK_CASES)
    def test_real_corpus_junk_not_in_content(self, monkeypatch, fixture, marker):
        junk = (FIXTURES / fixture).read_text(encoding="utf-8")
        content = _fetch(monkeypatch, _wrap(junk))
        assert marker not in content, f"junk leaked into content ({fixture})"
        assert "Actual readable text." in content

    def test_inline_script_block_stripped(self, monkeypatch):
        html = "<html><body><script>var secret='x=1'</script><p>Visible</p></body></html>"
        content = _fetch(monkeypatch, html)
        assert "secret" not in content
        assert "Visible" in content

    def test_inline_style_block_stripped(self, monkeypatch):
        html = "<html><body><style>body{color:red}</style><p>Visible</p></body></html>"
        content = _fetch(monkeypatch, html)
        assert "color:red" not in content
        assert "Visible" in content

    def test_script_with_src_attribute_kept_but_body_stripped(self, monkeypatch):
        html = (
            "<html><body>"
            "<script src='https://cdn.example.com/app.js'>inline junk()</script>"
            "<p>Visible</p></body></html>"
        )
        content = _fetch(monkeypatch, html)
        assert "inline junk" not in content
        assert "cdn.example.com" not in content
        assert "Visible" in content


class TestJunkOnlyPageYieldsNoContent:
    """J2 — a page with no extractable text yields empty content (ADR-0013:
    expected content absence is not an infrastructure failure); the extract
    CLI then stores nothing instead of a junk or empty node."""

    def test_js_only_page_returns_empty_content(self, monkeypatch):
        html = "<html><head><script>window.WIZ_global_data={}</script></head><body></body></html>"
        result = _fetch_result(monkeypatch, html)
        assert result.content == ""
        assert result.title is None

    def test_css_only_page_returns_empty_content(self, monkeypatch):
        html = "<html><head><style>:root{--fds-black:#000000}</style></head><body></body></html>"
        assert _fetch(monkeypatch, html) == ""

    def test_real_corpus_js_only_pages_yield_empty_content(self, monkeypatch):
        for fixture in ("google-slides-js.txt", "youtube-wiz-js.txt"):
            junk = (FIXTURES / fixture).read_text(encoding="utf-8")
            # The real pages were JS bundles with no meaningful text body.
            html = f"<html><head><script>{junk}</script></head><body></body></html>"
            assert _fetch(monkeypatch, html) == ""
