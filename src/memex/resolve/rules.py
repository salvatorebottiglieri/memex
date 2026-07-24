"""Deterministic URL resolution rules — classify a URL and return its type.

No LLM, no network.  Each rule matches a URL pattern and returns a
``Resolution`` describing what the URL represents and (when ingestable)
a ``direct_url`` suitable for fetching.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


# ── Public types ─────────────────────────────────────────────────────


@dataclass
class Resolution:
    """Result of URL resolution — describes what a URL represents."""

    url: str
    type: str
    ingestable: bool
    direct_url: str | None = None
    note: str | None = None


class ResolutionRule(ABC):
    """Abstract base for URL resolution rules."""

    @abstractmethod
    def match(self, url: str) -> Resolution | None: ...


# ── Built-in rules ───────────────────────────────────────────────────


class ArxivRule(ResolutionRule):
    """Matches arxiv.org/abs/ URLs and resolves to PDF."""

    _ARXIV_PATTERN = re.compile(
        r"^https?://arxiv\.org/abs/(\d+\.\d+)(v\d+)?"
    )

    def match(self, url: str) -> Resolution | None:
        m = self._ARXIV_PATTERN.match(url)
        if m:
            paper_id = m.group(1)
            version = m.group(2) or ""
            return Resolution(
                url=url,
                type="arxiv",
                ingestable=True,
                direct_url=f"https://arxiv.org/pdf/{paper_id}{version}",
            )
        return None


class GitHubBlobRule(ResolutionRule):
    """Matches github.com/{owner}/{repo}/blob/{branch}/{path}
    and resolves to raw content."""

    _PATTERN = re.compile(
        r"^https?://github\.com/"
        r"([^/]+)/([^/]+)/blob/([^/]+)/([^?#]+)"
    )

    def match(self, url: str) -> Resolution | None:
        m = self._PATTERN.match(url)
        if m:
            owner, repo, branch, path = m.groups()
            raw_url = (
                f"https://raw.githubusercontent.com/"
                f"{owner}/{repo}/{branch}/{path}"
            )
            return Resolution(
                url=url,
                type="github_file",
                ingestable=True,
                direct_url=raw_url,
            )
        return None


class WikipediaRule(ResolutionRule):
    """Matches *.wikipedia.org/wiki/{title} and resolves
    to the REST API summary endpoint."""

    _PATTERN = re.compile(
        r"^https?://([a-z][a-z-]*)\.wikipedia\.org/wiki/([^?#]+)"
    )

    def match(self, url: str) -> Resolution | None:
        m = self._PATTERN.match(url)
        if m:
            lang = m.group(1)
            title = m.group(2)
            api_url = (
                f"https://{lang}.wikipedia.org/"
                f"api/rest_v1/page/summary/{title}"
            )
            return Resolution(
                url=url,
                type="wikipedia",
                ingestable=True,
                direct_url=api_url,
            )
        return None


class MediaRule(ResolutionRule):
    """Matches URLs ending in media extensions or X/Twitter
    — not ingestable as text."""

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

        # X/Twitter URLs
        if host in ("x.com", "twitter.com"):
            return Resolution(
                url=url,
                type="unknown",
                ingestable=False,
                note="URL da social media: richiede browser per estrarre "
                "il link target",
            )

        # Media file extensions
        if any(path.endswith(ext) for ext in self._MEDIA_EXTENSIONS):
            return Resolution(
                url=url,
                type="unknown",
                ingestable=False,
                note="URL punta a un file multimediale "
                "(immagine/video/audio). "
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
            )
        return None


# ── Registry (injectable) ────────────────────────────────────────────

_default_rules: list[ResolutionRule] = [
    ArxivRule(),
    GitHubBlobRule(),
    WikipediaRule(),
    MediaRule(),
    DefaultRule(),
]

_rules: list[ResolutionRule] = list(_default_rules)


def get_rules() -> list[ResolutionRule]:
    """Return the active rule list (for injection in tests)."""
    return _rules


def set_rules(rules: list[ResolutionRule]) -> None:
    """Replace the active rule list (for tests)."""
    _rules[:] = rules


def reset_rules() -> None:
    """Restore default rules."""
    _rules[:] = _default_rules


# ── Internal helpers ─────────────────────────────────────────────────


def _strip_tracking_params(url: str) -> str:
    """Remove common tracking query parameters from a URL."""
    from urllib.parse import urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = {
        k: v
        for k, v in (p.split("=", 1) for p in parsed.query.split("&") if p)
    }
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


# ── Public API ───────────────────────────────────────────────────────


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
