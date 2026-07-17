"""Unit tests for memex.fetcher -- RoutingFetcher, PDFFetcher, FetchResult.

Tests dispatch logic, PDF extraction (with mocked pypdf), error
handling, and the content_path field.
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from memex.fetcher import (
    ContentFetcher,
    FetchError,
    FetchResult,
    HttpFetcher,
    PDFFetcher,
    RoutingFetcher,
    YouTubeTranscriptFetcher,
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

    def test_select_youtube_returns_yt_fetcher(self):
        router = self._router()
        f = router._select("youtube://abc123")
        assert isinstance(f, YouTubeTranscriptFetcher)

    def test_select_youtube_inherits_vault_path(self):
        router = RoutingFetcher([HttpFetcher, PDFFetcher], vault_path="/tmp/vault")
        f = router._select("youtube://abc123")
        assert isinstance(f, YouTubeTranscriptFetcher)
        assert f._vault_path == "/tmp/vault"
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
        """Happy path: download -> extract -> return content."""
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

# ── YouTubeTranscriptFetcher ────────────────────────────────────


class TestYouTubeTranscriptFetcher:
    """YouTubeTranscriptFetcher with mocked urllib and youtube-transcript-api."""

    @staticmethod
    def _mock_html_page(title: str = "My Test Video") -> MagicMock:
        """Return a mock urllib response with a YouTube watch page HTML."""
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = (
            f"<html><head><title>{title}</title></head>"
            f'<body><script>var ytInitialData = {{"channelName": "Test Channel"}};'
            f"</script></body></html>"
        ).encode("utf-8")
        return mock_resp

    def test_fetch_transcript_available_writes_cache(self, tmp_path):
        """Happy path: transcript available -> cache file written, metadata in content, content_path set."""
        mock_resp = self._mock_html_page()
        # Mock YouTubeTranscriptApi().fetch() returning snippet objects with .text
        snippet1 = MagicMock(text="Welcome to my video")
        snippet2 = MagicMock(text="This is a test")
        api_instance = MagicMock()
        api_instance.fetch.return_value = [snippet1, snippet2]
        fake_module = MagicMock()
        fake_module.YouTubeTranscriptApi = MagicMock(return_value=api_instance)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            import sys
            with patch.object(sys, "modules", {**sys.modules, "youtube_transcript_api": fake_module}):
                f = YouTubeTranscriptFetcher(vault_path=str(tmp_path))
                result = f.fetch("https://www.youtube.com/watch?v=abc123")

        assert result.title == "My Test Video"
        assert "# My Test Video" in result.content
        assert "Test Channel" in result.content
        assert result.content_path is not None
        assert result.content_path.endswith("youtube-abc123.md")
        # Cache file was written
        assert Path(result.content_path).exists()
        assert "Welcome to my video" in Path(result.content_path).read_text()

    def test_fetch_transcript_unavailable_metadata_only(self, tmp_path):
        """Transcript disabled/unavailable -> metadata only, no cache, content_path=None."""
        mock_resp = self._mock_html_page()
        exc_cls = type("TranscriptsDisabled", (Exception,), {})
        api_instance = MagicMock()
        api_instance.fetch.side_effect = exc_cls()
        fake_module = MagicMock()
        fake_module.YouTubeTranscriptApi = MagicMock(return_value=api_instance)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            import sys
            with patch.object(sys, "modules", {**sys.modules, "youtube_transcript_api": fake_module}):
                f = YouTubeTranscriptFetcher(vault_path=str(tmp_path))
                result = f.fetch("https://www.youtube.com/watch?v=abc123")

        assert result.title == "My Test Video"
        assert "# My Test Video" in result.content
        assert "Test Channel" in result.content
        assert result.content_path is None
        # No cache file written
        cache_dir = tmp_path / ".cache"
        assert not cache_dir.exists()

    def test_fetch_network_error_raises_fetch_error(self, tmp_path):
        """Network/rate limiting during transcript fetch -> FetchError."""
        mock_resp = self._mock_html_page()
        api_instance = MagicMock()
        api_instance.fetch.side_effect = Exception("HTTP Error 429: Too Many Requests")
        fake_module = MagicMock()
        fake_module.YouTubeTranscriptApi = MagicMock(return_value=api_instance)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            import sys
            with patch.object(sys, "modules", {**sys.modules, "youtube_transcript_api": fake_module}):
                f = YouTubeTranscriptFetcher(vault_path=str(tmp_path))
                with pytest.raises(FetchError, match="429"):
                    f.fetch("https://www.youtube.com/watch?v=abc123")

    def test_fetch_no_vault_path_includes_transcript_in_content(self, tmp_path):
        """When vault_path is None, include transcript in content, no cache."""
        mock_resp = self._mock_html_page()
        snippet = MagicMock(text="Inline transcript text")
        api_instance = MagicMock()
        api_instance.fetch.return_value = [snippet]
        fake_module = MagicMock()
        fake_module.YouTubeTranscriptApi = MagicMock(return_value=api_instance)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            import sys
            with patch.object(sys, "modules", {**sys.modules, "youtube_transcript_api": fake_module}):
                f = YouTubeTranscriptFetcher(vault_path=None)
                result = f.fetch("https://www.youtube.com/watch?v=abc123")

        assert result.title == "My Test Video"
        assert "# My Test Video" in result.content
        assert result.content_path is None
        assert "Inline transcript text" in result.content  # transcript included in content



    def test_fetch_missing_youtube_transcript_api(self, tmp_path):
        """Missing youtube-transcript-api raises FetchError with helpful message."""
        mock_resp = self._mock_html_page()
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "youtube_transcript_api":
                raise ImportError("No module named 'youtube_transcript_api'")
            return real_import(name, *args, **kwargs)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("builtins.__import__", side_effect=mock_import):
                f = YouTubeTranscriptFetcher(vault_path=str(tmp_path))
                with pytest.raises(FetchError, match="youtube-transcript-api"):
                    f.fetch("https://www.youtube.com/watch?v=abc123")
# ── Resolution rules ──────────────────────────────────────────────


class TestArxivRule:
    def test_matches_arxiv_abs_url(self):
        from memex.fetcher import ArxivRule
        rule = ArxivRule()
        result = rule.match("https://arxiv.org/abs/2304.12345")
        assert result is not None
        assert result.type == "arxiv"
        assert result.ingestable is True
        assert result.direct_url == "https://arxiv.org/pdf/2304.12345"

    def test_does_not_match_arxiv_pdf_url(self):
        from memex.fetcher import ArxivRule
        rule = ArxivRule()
        result = rule.match("https://arxiv.org/pdf/2304.12345")
        assert result is None

    def test_does_not_match_plain_http(self):
        from memex.fetcher import ArxivRule
        rule = ArxivRule()
        result = rule.match("https://example.com/article")
        assert result is None


class TestDefaultRule:
    def test_matches_any_http_url(self):
        from memex.fetcher import DefaultRule
        rule = DefaultRule()
        result = rule.match("https://example.com/article")
        assert result is not None
        assert result.type == "web"
        assert result.ingestable is True
        assert result.fetcher == "HttpFetcher"

    def test_matches_any_https_url(self):
        from memex.fetcher import DefaultRule
        rule = DefaultRule()
        result = rule.match("https://arxiv.org/pdf/2304.12345")
        assert result is not None
        assert result.type == "web"

    def test_does_not_match_ftp(self):
        from memex.fetcher import DefaultRule
        rule = DefaultRule()
        result = rule.match("ftp://example.com/file")
        assert result is None


class TestGitHubBlobRule:
    def test_matches_github_blob_url(self):
        from memex.fetcher import GitHubBlobRule

        rule = GitHubBlobRule()
        result = rule.match("https://github.com/user/repo/blob/main/file.py")
        assert result is not None
        assert result.type == "github_file"
        assert result.ingestable is True
        assert result.direct_url == "https://raw.githubusercontent.com/user/repo/main/file.py"

    def test_does_not_match_non_blob_github_url(self):
        from memex.fetcher import GitHubBlobRule

        rule = GitHubBlobRule()
        result = rule.match("https://github.com/user/repo")
        assert result is None

    def test_does_not_match_non_github_url(self):
        from memex.fetcher import GitHubBlobRule

        rule = GitHubBlobRule()
        result = rule.match("https://example.com")
        assert result is None

    def test_matches_github_blob_with_query_params(self):
        from memex.fetcher import GitHubBlobRule

        rule = GitHubBlobRule()
        result = rule.match("https://github.com/user/repo/blob/main/file.py?token=abc&ref=1")
        assert result is not None
        assert result.direct_url == "https://raw.githubusercontent.com/user/repo/main/file.py"

    def test_matches_github_blob_with_fragment(self):
        from memex.fetcher import GitHubBlobRule

        rule = GitHubBlobRule()
        result = rule.match("https://github.com/user/repo/blob/main/file.py#L42")
        assert result is not None
        assert result.direct_url == "https://raw.githubusercontent.com/user/repo/main/file.py"


class TestWikipediaRule:
    def test_matches_wikipedia_url(self):
        from memex.fetcher import WikipediaRule

        rule = WikipediaRule()
        result = rule.match("https://en.wikipedia.org/wiki/Python_(programming_language)")
        assert result is not None
        assert result.type == "wikipedia"
        assert result.ingestable is True
        assert result.direct_url == "https://en.wikipedia.org/api/rest_v1/page/summary/Python_(programming_language)"

    def test_matches_other_language(self):
        from memex.fetcher import WikipediaRule

        rule = WikipediaRule()
        result = rule.match("https://de.wikipedia.org/wiki/Python_(Programmiersprache)")
        assert result is not None
        assert result.direct_url == "https://de.wikipedia.org/api/rest_v1/page/summary/Python_(Programmiersprache)"

    def test_does_not_match_non_wikipedia_url(self):
        from memex.fetcher import WikipediaRule

        rule = WikipediaRule()
        result = rule.match("https://example.com")
        assert result is None

    def test_matches_hyphenated_language_code(self):
        from memex.fetcher import WikipediaRule

        rule = WikipediaRule()
        result = rule.match("https://zh-yue.wikipedia.org/wiki/%E7%B2%B5%E8%AA%9E")
        assert result is not None
        assert result.direct_url == "https://zh-yue.wikipedia.org/api/rest_v1/page/summary/%E7%B2%B5%E8%AA%9E"

    def test_matches_wikipedia_with_query_params(self):
        from memex.fetcher import WikipediaRule

        rule = WikipediaRule()
        result = rule.match("https://en.wikipedia.org/wiki/Python_(programming_language)?oldid=123")
        assert result is not None
        assert result.direct_url == "https://en.wikipedia.org/api/rest_v1/page/summary/Python_(programming_language)"


class TestResolveUrl:
    def test_resolve_arxiv_url(self):
        from memex.fetcher import resolve_url
        result = resolve_url("https://arxiv.org/abs/2304.12345")
        assert result.type == "arxiv"
        assert result.direct_url == "https://arxiv.org/pdf/2304.12345"

    def test_resolve_web_url(self):
        from memex.fetcher import resolve_url
        result = resolve_url("https://example.com/article")
        assert result.type == "web"
        assert result.ingestable is True

    def test_resolve_non_http_url_returns_error_type(self):
        from memex.fetcher import resolve_url
        result = resolve_url("ftp://example.com/file")
        assert result.type == "error"
        assert result.ingestable is False
