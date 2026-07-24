"""URL resolution rules + agent-based resolver for non-direct URLs.

Two layers:
1. Deterministic resolution rules (``resolve_url``) — classify a URL and tell
   an external agent what it is and how to fetch it. No LLM, no network.
2. Agent-based resolvers — spawn a browser (Playwright, Pi, custom) to
   extract the real target URL from social-media / login-walled pages.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass


# ── Deterministic URL resolution (was memex.fetcher) ─────────────


@dataclass
class Resolution:
    """Result of URL resolution — describes what a URL represents."""

    url: str
    type: str
    ingestable: bool
    fetcher: str | None = None
    direct_url: str | None = None
    note: str | None = None


class ResolutionRule(ABC):
    """Abstract base for URL resolution rules."""

    @abstractmethod
    def match(self, url: str) -> Resolution | None: ...


class ArxivRule(ResolutionRule):
    """Rule that matches arxiv.org/abs/ URLs and resolves to PDF."""

    _ARXIV_PATTERN = re.compile(r"^https?://arxiv\.org/abs/(\d+\.\d+)(v\d+)?")

    def match(self, url: str) -> Resolution | None:
        m = self._ARXIV_PATTERN.match(url)
        if m:
            paper_id = m.group(1)
            version = m.group(2) or ""
            return Resolution(
                url=url,
                type="arxiv",
                ingestable=True,
                fetcher="PDFFetcher",
                direct_url=f"https://arxiv.org/pdf/{paper_id}{version}",
            )
        return None


class GitHubBlobRule(ResolutionRule):
    """Rule that matches github.com/{owner}/{repo}/blob/{branch}/{path}
    and resolves to raw content."""

    _PATTERN = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/([^?#]+)")

    def match(self, url: str) -> Resolution | None:
        m = self._PATTERN.match(url)
        if m:
            owner, repo, branch, path = m.groups()
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            return Resolution(
                url=url,
                type="github_file",
                ingestable=True,
                fetcher="HttpFetcher",
                direct_url=raw_url,
            )
        return None


class WikipediaRule(ResolutionRule):
    """Rule that matches *.wikipedia.org/wiki/{title} and resolves
    to the REST API summary endpoint."""

    _PATTERN = re.compile(r"^https?://([a-z][a-z-]*)\.wikipedia\.org/wiki/([^?#]+)")

    def match(self, url: str) -> Resolution | None:
        m = self._PATTERN.match(url)
        if m:
            lang = m.group(1)
            title = m.group(2)
            api_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
            return Resolution(
                url=url,
                type="wikipedia",
                ingestable=True,
                fetcher="HttpFetcher",
                direct_url=api_url,
            )
        return None


class MediaRule(ResolutionRule):
    """Rule that matches URLs ending in media extensions or pointing
    to X/Twitter — not ingestable as text."""

    _MEDIA_EXTENSIONS = (
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
        ".mp4", ".webm", ".avi", ".mov", ".mkv",
        ".mp3", ".wav", ".ogg", ".flac",
    )

    def match(self, url: str) -> Resolution | None:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path.lower()
        host = parsed.hostname or ""

        # Check for X/Twitter URLs
        if host in ("x.com", "twitter.com"):
            return Resolution(
                url=url,
                type="unknown",
                ingestable=False,
                note="URL da social media: richiede browser per estrarre il link target",
            )

        # Check for media file extensions
        if any(path.endswith(ext) for ext in self._MEDIA_EXTENSIONS):
            return Resolution(
                url=url,
                type="unknown",
                ingestable=False,
                note="URL punta a un file multimediale (immagine/video/audio). "
                "memex supporta solo fonti testuali.",
            )

        return None


class DefaultRule(ResolutionRule):
    """Fallback rule: matches any http/https URL as a generic web page."""

    def match(self, url: str) -> Resolution | None:
        if url.startswith(("http://", "https://")):
            return Resolution(
                url=url,
                type="web",
                ingestable=True,
                fetcher="HttpFetcher",
            )
        return None


_rules: list[ResolutionRule] = [
    ArxivRule(), GitHubBlobRule(), WikipediaRule(), MediaRule(), DefaultRule(),
]


def _strip_tracking_params(url: str) -> str:
    """Remove common tracking query parameters from a URL."""
    from urllib.parse import urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = dict(p.split("=", 1) for p in parsed.query.split("&") if p)
    _TRACKING_EXACT = frozenset({
        "fbclid", "gclid", "ref", "source", "mc_cid", "mc_eid",
    })
    clean = {
        k: v
        for k, v in params.items()
        if not k.startswith("utm_") and k not in _TRACKING_EXACT
    }
    if len(clean) == len(params):
        return url
    return urlunparse(parsed._replace(query=urlencode(clean)))


def resolve_url(url: str) -> Resolution:
    """Apply resolution rules and return the first match.

    Tracking parameters are stripped before classification.
    """
    clean_url = _strip_tracking_params(url)
    for rule in _rules:
        result = rule.match(clean_url)
        if result is not None:
            if result.direct_url:
                result = Resolution(
                    url=url,
                    type=result.type,
                    ingestable=result.ingestable,
                    fetcher=result.fetcher,
                    direct_url=result.direct_url,
                    note=result.note,
                )
            return result
    return Resolution(
        url=url,
        type="error",
        ingestable=False,
        note=f"No rule matched URL: {url}",
    )


# ── Agent-based resolver (agent visits URL in a browser) ─────────


class ResolverError(Exception):
    """Raised when URL resolution via agent fails."""


class Resolver(ABC):
    """Abstract base for agent-based URL resolvers."""

    @abstractmethod
    def resolve(self, url: str) -> str:
        """Open *url* in a browser and return the resolved target URL."""

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Return True if this resolver's CLI is installed."""


class PlaywrightResolver(Resolver):
    """Resolver that uses Playwright (headless Chromium) to resolve URLs.

    Opens the URL in a headless browser, waits for the page, and extracts
    the actual content URL from meta tags (og:url, twitter:url) or redirects.
    """

    @classmethod
    def available(cls) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    def resolve(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ResolverError("Playwright is not installed")

        try:
            cookies_file = os.environ.get("MEMEX_COOKIES_FILE")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context()

                if cookies_file and os.path.isfile(cookies_file):
                    import json as _json
                    try:
                        with open(cookies_file) as f:
                            cookies = _json.load(f)
                            ctx.add_cookies(
                                cookies if isinstance(cookies, list) else [cookies]
                            )
                    except Exception as e:
                        raise ResolverError(
                            f"Failed to load cookies from {cookies_file}: {e}"
                        )

                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                resolved = page.evaluate("""() => {
                    const skipDomains = ['x.com', 'twitter.com', 'facebook.com',
                                         'instagram.com', 'tiktok.com'];
                    const isSocial = (u) => skipDomains.some(d => u.includes(d));
                    const cur = document.URL.toLowerCase();
                    if (cur.includes('/login') || cur.includes('/onboarding')
                        || cur.includes('/auth')) {
                        return '__LOGIN_REQUIRED__';
                    }
                    const og = document.querySelector('meta[property="og:url"]');
                    if (og && !isSocial(og.content)) return og.content;
                    const tw = document.querySelector('meta[name="twitter:url"]');
                    if (tw && !isSocial(tw.content)) return tw.content;
                    for (const a of document.querySelectorAll('a[href*="t.co/"]')) {
                        if (a.href.startsWith('http')) return a.href;
                    }
                    for (const a of document.querySelectorAll('a[href]')) {
                        const h = a.href;
                        if (h.startsWith('http') && !isSocial(h)
                            && !h.includes('/intent/')) return h;
                    }
                    return document.URL;
                }""")

                final_url = page.url
                browser.close()

                if resolved == '__LOGIN_REQUIRED__':
                    msg = "Login required."
                    if not cookies_file:
                        msg += (
                            " Set MEMEX_COOKIES_FILE to a cookies.json file"
                            " with your session."
                        )
                    else:
                        msg += f" Cookies from {cookies_file} expired or invalid."
                    raise ResolverError(msg)

                result = resolved or final_url
                if result.startswith(("http://", "https://")):
                    return result
                raise ResolverError(f"No valid URL extracted from {url}")
        except ResolverError:
            raise
        except Exception as e:
            raise ResolverError(str(e))


class PiResolver(Resolver):
    """Resolver that uses the Pi agent CLI (non-interactive mode)."""

    PROMPT = (
        "You have a browser available. "
        "Open this URL in the browser: {url} . "
        "Find the actual article/content link it redirects to or contains. "
        "Return ONLY the resolved URL as plain text. Nothing else."
    )

    @classmethod
    def available(cls) -> bool:
        return shutil.which("pi") is not None

    def resolve(self, url: str) -> str:
        prompt = self.PROMPT.format(url=url)
        try:
            result = subprocess.run(
                ["pi", "-p", prompt],
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout.strip()
            if output and (output.startswith("http://") or output.startswith("https://")):
                return output.strip().rstrip(".").rstrip(",")
            raise ResolverError(f"Pi did not return a valid URL: {output[:200]}")
        except subprocess.TimeoutExpired:
            raise ResolverError("Pi timed out resolving URL")
        except FileNotFoundError:
            raise ResolverError("Pi CLI not found")


class _CustomResolver(Resolver):
    """Resolver from MEMEX_RESOLVER_CMD env var."""

    def __init__(self, cmd: str):
        self._cmd = cmd

    @classmethod
    def available(cls) -> bool:
        return True

    def resolve(self, url: str) -> str:
        try:
            result = subprocess.run(
                self._cmd.split() + [url],
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout.strip()
            if output and (output.startswith("http://") or output.startswith("https://")):
                return output.strip().rstrip(".").rstrip(",")
            raise ResolverError(
                f"Custom resolver did not return a valid URL: {output[:200]}"
            )
        except subprocess.TimeoutExpired:
            raise ResolverError("Custom resolver timed out")
        except FileNotFoundError:
            raise ResolverError("Custom resolver not found")


_RESOLVERS: list[type[Resolver]] = [PlaywrightResolver, PiResolver]


def detect_resolver() -> Resolver | None:
    """Return the first available resolver, or None."""
    custom_cmd = os.environ.get("MEMEX_RESOLVER_CMD")
    if custom_cmd:
        return _CustomResolver(custom_cmd)
    for cls in _RESOLVERS:
        if cls.available():
            return cls()
    return None
