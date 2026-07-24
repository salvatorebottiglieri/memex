"""Agent-based URL resolvers — visit a URL in a browser and return the
real target URL for social-media / login-walled pages.

Resolvers are tried in priority order: custom env command → Playwright
→ Pi CLI.  ``detect_resolver()`` returns the first available one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod


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


# ── Concrete resolvers ───────────────────────────────────────────────


class PlaywrightResolver(Resolver):
    """Resolver that uses Playwright (headless Chromium) to resolve URLs."""

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
                                cookies
                                if isinstance(cookies, list)
                                else [cookies]
                            )
                    except Exception as e:
                        raise ResolverError(
                            f"Failed to load cookies from "
                            f"{cookies_file}: {e}"
                        )

                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                resolved = page.evaluate("""() => {
                    const skipDomains = ['x.com', 'twitter.com',
                        'facebook.com', 'instagram.com', 'tiktok.com'];
                    const isSocial = (u) =>
                        skipDomains.some(d => u.includes(d));
                    const cur = document.URL.toLowerCase();
                    if (cur.includes('/login')
                        || cur.includes('/onboarding')
                        || cur.includes('/auth')) {
                        return '__LOGIN_REQUIRED__';
                    }
                    const og = document.querySelector(
                        'meta[property="og:url"]');
                    if (og && !isSocial(og.content)) return og.content;
                    const tw = document.querySelector(
                        'meta[name="twitter:url"]');
                    if (tw && !isSocial(tw.content)) return tw.content;
                    for (const a of
                        document.querySelectorAll('a[href*="t.co/"]')) {
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
                        msg += (
                            f" Cookies from {cookies_file} "
                            "expired or invalid."
                        )
                    raise ResolverError(msg)

                result = resolved or final_url
                if result.startswith(("http://", "https://")):
                    return result
                raise ResolverError(
                    f"No valid URL extracted from {url}"
                )
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
            if output and (
                output.startswith("http://")
                or output.startswith("https://")
            ):
                return output.strip().rstrip(".").rstrip(",")
            raise ResolverError(
                f"Pi did not return a valid URL: {output[:200]}"
            )
        except subprocess.TimeoutExpired:
            raise ResolverError("Pi timed out resolving URL")
        except FileNotFoundError:
            raise ResolverError("Pi CLI not found")


class _CustomResolver(Resolver):
    """Resolver from MEMEX_RESOLVER_CMD env var."""

    def __init__(self, cmd: str) -> None:
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
            if output and (
                output.startswith("http://")
                or output.startswith("https://")
            ):
                return output.strip().rstrip(".").rstrip(",")
            raise ResolverError(
                f"Custom resolver did not return a valid URL: "
                f"{output[:200]}"
            )
        except subprocess.TimeoutExpired:
            raise ResolverError("Custom resolver timed out")
        except FileNotFoundError:
            raise ResolverError("Custom resolver not found")


# ── Priority-ordered resolver list ───────────────────────────────────

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
