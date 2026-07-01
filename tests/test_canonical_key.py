"""Tests for the canonical_key pure function.

canonical_key is the dedup identity of a source: a normalized URL with
tracking params stripped (utm_*, fbclid, etc.), scheme/host normalized,
and known platforms mapped to stable URI schemes.
"""
import pytest

from memex.canonical_key import canonical_key


class TestBasicNormalization:
    def test_strips_utm_params(self):
        url = "https://example.com/article?utm_source=twitter&utm_medium=social"
        assert canonical_key(url) == "https://example.com/article"

    def test_strips_fbclid(self):
        url = "https://example.com/page?fbclid=IwAR123abc"
        assert canonical_key(url) == "https://example.com/page"

    def test_strips_multiple_tracking_params_preserves_real_ones(self):
        url = "https://example.com/search?q=python&utm_campaign=test&fbclid=abc"
        assert canonical_key(url) == "https://example.com/search?q=python"

    def test_lowercases_scheme_and_host(self):
        url = "HTTPS://Example.COM/Path"
        assert canonical_key(url) == "https://example.com/Path"

    def test_plain_url_unchanged(self):
        url = "https://example.com/article"
        assert canonical_key(url) == "https://example.com/article"

    def test_removes_default_http_port(self):
        url = "http://example.com:80/page"
        assert canonical_key(url) == "http://example.com/page"

    def test_removes_default_https_port(self):
        url = "https://example.com:443/page"
        assert canonical_key(url) == "https://example.com/page"

    def test_strips_fragment(self):
        url = "https://example.com/article#section-2"
        assert canonical_key(url) == "https://example.com/article"


class TestYouTube:
    def test_youtube_watch_url_maps_to_youtube_scheme(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert canonical_key(url) == "youtube://dQw4w9WgXcQ"

    def test_youtu_be_shortlink_maps_to_youtube_scheme(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert canonical_key(url) == "youtube://dQw4w9WgXcQ"

    def test_youtube_with_tracking_params_maps_to_youtube_scheme(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=newsletter"
        assert canonical_key(url) == "youtube://dQw4w9WgXcQ"

    def test_youtube_with_timestamp_maps_to_youtube_scheme(self):
        """The t= timestamp is a non-structural param; canonical key is just the video id."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120"
        assert canonical_key(url) == "youtube://dQw4w9WgXcQ"


class TestTrailingSlash:
    def test_trailing_slash_on_root_preserved(self):
        """Root paths keep their slash per RFC normalization."""
        url = "https://example.com/"
        assert canonical_key(url) == "https://example.com/"

    def test_trailing_slash_on_path_stripped(self):
        url = "https://example.com/article/"
        assert canonical_key(url) == "https://example.com/article"
