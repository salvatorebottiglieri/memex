"""canonical_key — pure function mapping a raw URL to its dedup identity.

Rules applied in order:
1. Map known platforms to stable URI schemes (e.g. youtube://<id>).
2. Lowercase scheme and host.
3. Strip default ports (80 for http, 443 for https).
4. Strip fragment.
5. Strip tracking query params (utm_*, fbclid, gclid, ref, …).
6. Strip trailing slash from non-root paths.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# Query params considered tracking noise — stripped unconditionally.
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "_ga",
        "yclid",
        "igshid",
        "s_cid",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _is_tracking_param(key: str) -> bool:
    if key in _TRACKING_EXACT:
        return True
    return any(key.startswith(prefix) for prefix in _TRACKING_PREFIXES)


def _strip_tracking(query: str) -> str:
    params = parse_qs(query, keep_blank_values=True)
    clean = {k: v for k, v in params.items() if not _is_tracking_param(k)}
    return urlencode(clean, doseq=True)


def _youtube_id(parsed) -> str | None:
    """Return the YouTube video id if this URL is a YouTube watch page, else None."""
    host = parsed.netloc.lower()
    if host in ("www.youtube.com", "youtube.com"):
        if parsed.path == "/watch":
            params = parse_qs(parsed.query)
            ids = params.get("v")
            if ids:
                return ids[0]
    if host == "youtu.be":
        vid = parsed.path.lstrip("/")
        if vid:
            return vid
    return None


def canonical_key(url: str) -> str:
    """Return the canonical key (dedup identity) for *url*.

    This is a pure function: same input always produces the same output.
    """
    parsed = urlparse(url)

    # --- Platform-specific mappings ---
    yt_id = _youtube_id(parsed)
    if yt_id is not None:
        return f"youtube://{yt_id}"

    # --- Normalize scheme/host (lowercase) ---
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Strip default ports
    if ":" in netloc:
        host, port_str = netloc.rsplit(":", 1)
        try:
            port = int(port_str)
            if _DEFAULT_PORTS.get(scheme) == port:
                netloc = host
        except ValueError:
            pass

    # --- Strip fragment ---
    fragment = ""

    # --- Strip tracking query params ---
    clean_query = _strip_tracking(parsed.query)

    # --- Strip trailing slash from non-root paths ---
    path = parsed.path
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")

    normalized = urlunparse((scheme, netloc, path, parsed.params, clean_query, fragment))
    return normalized
