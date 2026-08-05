"""Stress — adversarial fetcher inputs (synthetic).

A1  login-wall / CSS-heavy pages: only the page's real text survives;
    stylesheet payloads never reach the content
A2  the <title> extraction keeps working after script/style stripping
A3  empty or whitespace-only pages produce no storable content

Download-cap behavior (oversized Content-Length, runaway streaming) is
already covered by tests/test_extract_command.py — not duplicated here.
"""
from __future__ import annotations

import pytest

from memex.fetchers import FetchError
from memex.fetchers.http import HttpFetcher


def _fetch(monkeypatch, html: str) -> str:
    monkeypatch.setattr(
        "memex.fetchers.http.download_bytes",
        lambda url: html.encode("utf-8"),
    )
    return HttpFetcher().fetch("https://example.com/page").content


class TestLoginWallAndCssHeavy:
    def test_login_wall_css_block_not_in_content(self, monkeypatch):
        html = (
            "<html><head><style>:root,.__ig-light-mode:root{--fds-black:#000"
            "--fds-white:#fff}</style></head>"
            "<body><p>Log in to continue.</p></body></html>"
        )
        content = _fetch(monkeypatch, html)
        assert "--fds-black" not in content
        assert "Log in to continue." in content

    def test_style_in_head_and_inline_style_attribute(self, monkeypatch):
        html = (
            "<html><head><style>body{background:url(https://x/y.png)}</style></head>"
            '<body><p style="color:red">Text with inline style</p></body></html>'
        )
        content = _fetch(monkeypatch, html)
        assert "background:url" not in content
        assert "Text with inline style" in content

    def test_script_tag_with_cdata_section(self, monkeypatch):
        html = (
            "<html><body><script><![CDATA[var a=1;</script><p>Visible</p></body></html>"
        )
        content = _fetch(monkeypatch, html)
        assert "var a=1" not in content
        assert "Visible" in content

    def test_empty_page_raises(self, monkeypatch):
        with pytest.raises(FetchError):
            _fetch(monkeypatch, "<html><head></head><body></body></html>")

    def test_whitespace_only_page_raises(self, monkeypatch):
        with pytest.raises(FetchError):
            _fetch(monkeypatch, "<html><body>   \n\t  </body></html>")


class TestTitleSurvivesStripping:
    def test_title_extracted_from_junk_page(self, monkeypatch):
        html = (
            "<html><head><title>Lipari Summer School 2026</title>"
            "<script>window['ppConfig']={}</script></head>"
            "<body><p>Actual content.</p></body></html>"
        )
        monkeypatch.setattr(
            "memex.fetchers.http.download_bytes",
            lambda url: html.encode("utf-8"),
        )
        result = HttpFetcher().fetch("https://example.com/page")
        assert result.title == "Lipari Summer School 2026"
        assert "ppConfig" not in result.content
        assert "Actual content." in result.content
