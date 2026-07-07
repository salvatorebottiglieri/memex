"""Unit tests for memex.fetcher — RoutingFetcher, PDFFetcher, FetchResult.

Tests dispatch logic, PDF extraction (with mocked pypdf), error
handling, and the content_path field.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from memex.fetcher import (
    ContentFetcher,
    FetchError,
    FetchResult,
    HttpFetcher,
    PDFFetcher,
    RoutingFetcher,
    load_fetcher,
)


# ── FetchResult content_path ──────────────────────────────────────


class TestFetchResultContentPath:
    def test_content_path_defaults_to_none(self):
        r = FetchResult(content="hello")
        assert r.content_path is None

    def test_content_path_can_be_set(self):
        r = FetchResult(content="hello", content_path="/tmp/test.md")
        assert r.content_path == "/tmp/test.md"

    def test_backward_compatible_no_content_path(self):
        """Existing callers that pass content + title continue working."""
        r = FetchResult(content="hello", title="Test")
        assert r.content == "hello"
        assert r.title == "Test"
        assert r.content_path is None


# ── RoutingFetcher._select dispatch ──────────────────────────────


class TestRoutingFetcherSelect:
    def _router(self):
        return RoutingFetcher([HttpFetcher, PDFFetcher])

    def test_select_pdf_https(self):
        router = self._router()
        f = router._select("https://example.com/paper.pdf")
        assert isinstance(f, PDFFetcher)

    def test_select_pdf_http(self):
        router = self._router()
        f = router._select("http://example.com/report.pdf")
        assert isinstance(f, PDFFetcher)

    def test_select_pdf_with_trailing_slash(self):
        router = self._router()
        f = router._select("https://example.com/doc.pdf/")
        assert isinstance(f, PDFFetcher)

    def test_select_regular_http(self):
        router = self._router()
        f = router._select("https://example.com/article")
        assert isinstance(f, HttpFetcher)

    def test_select_regular_https(self):
        router = self._router()
        f = router._select("http://example.com/page.html")
        assert isinstance(f, HttpFetcher)

    def test_select_youtube_prefix(self):
        router = self._router()
        with pytest.raises(FetchError, match="not yet implemented"):
            router._select("youtube://abc123")

    def test_select_pdf_query_string(self):
        router = self._router()
        f = router._select("https://example.com/paper.pdf?download=1")
        assert isinstance(f, PDFFetcher)


# ── RoutingFetcher.fetch (delegation) ────────────────────────────


class TestRoutingFetcherFetch:
    def test_fetch_delegates_to_selected_fetcher(self):
        """Smoke test: RoutingFetcher.fetch calls the selected fetcher."""
        mock_fetcher = MagicMock(spec=ContentFetcher)
        mock_fetcher.fetch.return_value = FetchResult(content="mocked")

        class MockRouter(RoutingFetcher):
            def _select(self, ckey):
                return mock_fetcher

        router = MockRouter([HttpFetcher])
        result = router.fetch("https://example.com/doc")
        assert result.content == "mocked"
        mock_fetcher.fetch.assert_called_once_with("https://example.com/doc")

    def test_fetch_propagates_fetch_error(self):
        mock_fetcher = MagicMock(spec=ContentFetcher)
        mock_fetcher.fetch.side_effect = FetchError("boom")

        class MockRouter(RoutingFetcher):
            def _select(self, ckey):
                return mock_fetcher

        router = MockRouter([HttpFetcher])
        with pytest.raises(FetchError, match="boom"):
            router.fetch("https://example.com/doc")


# ── PDFFetcher.fetch ─────────────────────────────────────────────


class TestPDFFetcherFetch:
    """PDFFetcher with mocked urllib and pypdf."""

    @patch("urllib.request.urlopen")
    @patch("pypdf.PdfReader")
    def test_fetch_success(self, mock_reader, mock_urlopen):
        """Happy path: download → extract → return content."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = b"%PDF-1.4 data"
        mock_urlopen.return_value = mock_resp

        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Extracted PDF text."
        mock_reader.return_value.pages = [mock_page]

        f = PDFFetcher()
        result = f.fetch("https://example.com/doc.pdf")
        assert result.content == "Extracted PDF text."
        assert result.title is None

    @patch("urllib.request.urlopen")
    @patch("pypdf.PdfReader")
    def test_fetch_multiple_pages(self, mock_reader, mock_urlopen):
        """Multiple pages are concatenated."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = b"%PDF-1.4 data"
        mock_urlopen.return_value = mock_resp

        p1 = MagicMock(); p1.extract_text.return_value = "Page 1."
        p2 = MagicMock(); p2.extract_text.return_value = "Page 2."
        mock_reader.return_value.pages = [p1, p2]

        f = PDFFetcher()
        result = f.fetch("https://example.com/doc.pdf")
        assert "Page 1." in result.content
        assert "Page 2." in result.content

    @patch("urllib.request.urlopen")
    def test_fetch_network_error(self, mock_urlopen):
        """Network failures become FetchError."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        f = PDFFetcher()
        with pytest.raises(FetchError, match="connection refused"):
            f.fetch("https://example.com/doc.pdf")

    @patch("urllib.request.urlopen")
    @patch("pypdf.PdfReader")
    def test_fetch_pdf_read_error(self, mock_reader, mock_urlopen):
        """PdfReader failures (encrypted, corrupt) become FetchError."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = b"%PDF-1.4 data"
        mock_urlopen.return_value = mock_resp
        mock_reader.side_effect = Exception("File has not been decrypted")

        f = PDFFetcher()
        with pytest.raises(FetchError, match="File has not been decrypted"):
            f.fetch("https://example.com/doc.pdf")

    @patch("urllib.request.urlopen")
    def test_fetch_missing_pypdf(self, mock_urlopen):
        """Lazy import failure raises FetchError with helpful message."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = b"%PDF-1.4 data"
        mock_urlopen.return_value = mock_resp

        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError("No module named 'pypdf'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            f = PDFFetcher()
            with pytest.raises(FetchError, match="pypdf"):
                f.fetch("https://example.com/doc.pdf")

    @patch("urllib.request.urlopen")
    def test_fetch_timeout(self, mock_urlopen):
        """Timeout during download becomes FetchError."""
        import socket
        mock_urlopen.side_effect = socket.timeout("timed out")

        f = PDFFetcher()
        with pytest.raises(FetchError, match="timed out"):
            f.fetch("https://example.com/doc.pdf")


# ── load_fetcher ─────────────────────────────────────────────────


class TestLoadFetcher:
    def test_load_fetcher_default_returns_routing(self):
        f = load_fetcher()
        assert isinstance(f, RoutingFetcher)

    def test_load_fetcher_module_override(self):
        """Passing a module path still delegates to load_class."""
        f = load_fetcher("tests.conftest:FakeFetcher")
        assert type(f).__name__ == "FakeFetcher"

    def test_routing_fetcher_has_http_and_pdf(self):
        router = load_fetcher()
        assert len(router._fetchers) == 2
        assert HttpFetcher in router._fetchers
        assert PDFFetcher in router._fetchers
