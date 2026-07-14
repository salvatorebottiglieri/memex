# Fetcher router with per-type extractors (YouTube, PDF)

## Problem Statement

Today, `memex ingest <url>` only works for HTML articles. The `HttpFetcher` downloads any URL as HTML and strips tags — it's a single extractor. This fails for two common source types:

- **YouTube videos**: the URL resolves to a YouTube watch page whose HTML contains no transcript text. An ingested YouTube URL produces an L0 with metadata (title, channel) but no substantive content, so no derivation can be built from it.
- **PDFs**: a `.pdf` URL downloads binary data, not HTML. The `<title>` regex and tag-stripping produce garbled output or an empty L0.

Both are first-class sources the user saves to their inbox. Without per-type extraction, they hit a dead end in the ingestion pipeline: ingested but not useful.

## Solution

Add a **routing fetcher** (`RoutingFetcher`) that dispatches URLs to the right extractor based on the `canonical_key` prefix. For YouTube, a `YouTubeTranscriptFetcher` fetches the transcript via `youtube-transcript-api`, stores it in `$VAULT/.cache/`, and writes metadata-only to the L0 markdown file. For PDFs, a `PDFFetcher` extracts text via `pypdf` and writes it to the L0 markdown file directly (same as `HttpFetcher` but with a different backend). The router is transparent to all existing callers — `ingest_single_url`, `_derive_single`, and `memex ingest` — because the interface remains `ContentFetcher.fetch(url) -> FetchResult`.

The `FetchResult` dataclass gains an optional `content_path` field for fetchers that produce artifacts outside the L0 markdown file (transcripts, audio -> text). The ingester stores this in the `node.content_path` column, and the deriver reads it when it needs the full content.

## User Stories

1. As a user, I want to `memex ingest https://youtube.com/watch?v=ABC123` and get an L0 node with title + channel metadata, so that I have a record of the video in my graph.
2. As a user, I want the YouTube transcript to be fetched and cached when available, so that the agent can derive notes-tier summaries and synthesis from the video content.
3. As a user, I want a YouTube video with no transcript (disabled, private, or non-English without captions) to still be ingested as a metadata-only L0, so that I don't lose the reference.
4. As a user, I want `memex derive <yt-l0-id>` on a YouTube video to produce a notes-tier derivation even when no transcript is available (metadata-only derive, graceful failure if content is too thin), so that the pipeline does not crash.
5. As a user, I want a re-ingest of the same YouTube URL to hit the existing cache and produce `already_exists`, so that the transcript is not fetched twice.
6. As a user, I want the cached transcript to be immutable once written, so that a re-derive does not need to re-fetch YouTube.
7. As a user, I want to manually delete `$VAULT/.cache/youtube-<id>.md` to force a fresh transcript fetch, so that I can recover from stale or corrupted cache files.
8. As a user, I want to `memex ingest https://example.com/paper.pdf` and get an L0 node with extracted text from the PDF, so that research papers and PDF documents enter the graph as usable raw sources.
9. As a user, I want a PDF whose text extraction fails (encrypted, image-only scan) to be ingested with empty content and a `failed` source row, so that the pipeline does not crash and the reference is preserved.
10. As a user, I want the existing `HttpFetcher` to remain the default for all other URLs, so that HTML articles continue to work unchanged.
11. As a user, I want `memex derive` on a PDF L0 to produce a notes-tier derivation from the extracted text, so that PDF sources are fully usable.
12. As a user, I want the routing to be based on the same `canonical_key` prefix that dedup already uses, so that YouTube URLs are recognised by their `youtube://` canonical form and PDF URLs by their `.pdf` path extension.
13. As a user, I want no new CLI flags or interface changes — `memex ingest <url>` just works for the new types, so that the learning curve is zero.
14. As a user, I want the `content_path` field in `FetchResult` to be opt-in (only set by YouTubeTranscriptFetcher), so that HttpFetcher and PDFFetcher remain simple and do not create cache files.

## Implementation Decisions

### RoutingFetcher

A `RoutingFetcher` class wraps all available fetchers and dispatches based on the canonical key prefix (computed by `memex.canonical_key.canonical_key()`):

```
RoutingFetcher.fetch(url):
    ckey = canonical_key(url)
    fetcher = self._select(ckey)
    return fetcher.fetch(url)
```

The routing table (`_select`) maps canonical key prefixes to fetcher classes:

| ckey prefix | Fetcher |
|---|---|
| `youtube://` | `YouTubeTranscriptFetcher` |
| `http[s]://` -> URL ends with `.pdf` | `PDFFetcher` |
| `http[s]://` (default) | `HttpFetcher` |

The router is composed at construction time from a `{prefix: FetcherClass}` dict. The existing `load_fetcher` function returns the router when no `MEMEX_FETCHER_MODULE` override is set, maintaining backward compatibility with test injection.

### FetchResult.content_path

The `FetchResult` dataclass gains an optional `content_path` field:

- `YouTubeTranscriptFetcher`: `content` = metadata (title, channel), `content_path` = path to cached transcript
- `PDFFetcher`: `content` = extracted text, `content_path` = None
- `HttpFetcher`: `content` = HTML-to-markdown, `content_path` = None

The ingester's `ingest_single_url` stores `content_path` in `node.content_path`. The deriver's `_do_derive` reads `node.content_path` to find the full content for the agent.

### Cache location

Per-type artifacts live in `$VAULT/.cache/` with the convention `<type>-<canonical-id>.md`:

- YouTube transcript: `$VAULT/.cache/youtube-<video-id>.md`

The cache is **immutable once written**: re-derive reuses it. Delete the file manually to force a refresh.

### YouTubeTranscriptFetcher

- **Transcript available**: writes to cache, returns metadata in `content`, sets `content_path`
- **Transcript disabled / unavailable**: returns metadata in `content`, `content_path = None`
- **Network / rate limiting**: raises `FetchError` (same as all fetchers — infrastructure failure)

The derive caller checks `content_path`: if None and the node has no L0 markdown file (metadata-only), derive produces a graceful failure rather than crashing.

### PDFFetcher

- Extracts text via `pypdf`
- Returns extracted text in `content` as a plain-text markdown block
- Raises `FetchError` for binary/encrypted/network failures

### Caller changes

- `load_fetcher(default_backend)` returns `RoutingFetcher()` by default (instead of `HttpFetcher()`)
- All callers (`ingest_single_url`, `_do_derive`, `_derive_all_inner`) already use `fetcher.fetch(url)` — no signature changes
- `ingest_single_url` already stores `content_path` in `store.create_node()` — the column exists, no schema change needed

### Dependencies

- `youtube-transcript-api` — Python package for fetching YouTube transcript text
- `pypdf` — Python package for PDF text extraction

Both are optional runtime dependencies: `load_fetcher()` only imports them when the relevant URL type is ingested. A `pip install` of both is added to `pyproject.toml` as optional extras (`pip install memex[media]` or `uv add memex[media]`).

### No schema changes

The `node.content_path` column already exists (ADR-0008). No migration needed. The `FetchResult.content_path` is passed through `ingest_single_url` -> `store.create_node(content_path=...)`.

### YouTube dedup

The `canonical_key` function already returns `youtube://<id>` for YouTube URLs (implemented). The routing dispatching key is this canonical prefix, not the raw URL.

## Testing Decisions

**What makes a good test for this feature:** the test asserts on the observable output of `ingest_single_url` — which L0 content is written, what `content_path` is stored, and whether the correct fetcher class dispatched — not on the internal routing mechanics. For individual extractors, the test asserts on `FetchResult` fields (content shape, content_path, title) for given input conditions.

**Modules tested:**

- `src/memex/fetcher.py` — `RoutingFetcher` dispatch table, `YouTubeTranscriptFetcher` (with `youtube-transcript-api` replaced by a mock), `PDFFetcher` (with `pypdf` replaced by a mock), the `FetchResult.content_path` field, `load_fetcher` default return value.
- `src/memex/ingester.py` — `ingest_single_url` with a `RoutingFetcher` wrapping mock extractors: verify that a YouTube URL writes metadata-only L0 + cache content_path, and that a PDF URL writes extracted-text L0.
- End-to-end via smoke test: a `smoke_fetcher_youtube` test in `smoke_test.py` that uses `MEMEX_FETCHER_MODULE` to inject a fake fetcher returning YouTube-shaped content and asserts the L0 node has `content_path` and metadata content. A `smoke_fetcher_pdf` test that does the same for PDF-shaped content.

**Prior art:**

- `tests/test_ingester.py` — tests `ingest_single_url` with a `FakeFetcher` class. The new fetcher-router tests follow the same pattern: inject a `RoutingFetcher` with mock extractors and assert on the result dict (status, content_path, title).
- `tests/smoke_test.py` has `smoke_youtube` (lines 541-558) that tests the canonical key mapping only. A new `smoke_fetcher_youtube` tests the full ingest path with a fake fetcher module.
- The `MEMEX_FETCHER_MODULE` env var pattern is documented and used by `smoke_derive_passing` (line 283) and `smoke_derive_failing` (line 328).

## Out of Scope

- Tweets/X extraction: the canonical key mapping exists in `canonical_key.py` but the extractor is deferred to a follow-up.
- Audio / podcast transcription: no audio-to-text pipeline.
- Vimeo, Twitch, or other video platforms: only YouTube is targeted.
- Cache GC policy: no automatic cleanup of `$VAULT/.cache/`. Manual deletion.
- Re-fetch of cached transcripts on staleness/contested: the trust-cascade path (ADR-0014) does not trigger re-fetch.
- Image extraction from PDFs / OCR: `pypdf` text extraction only.
- YouTube playlist or channel URLs: single video watch URLs only (`/watch?v=ID` and `youtu.be/ID`).

## Further Notes

- The `canonical_key` prefix is the routing key, not the raw URL. This means YouTube short links (`youtu.be/ID`) and full watch URLs (`youtube.com/watch?v=ID`) both route to `YouTubeTranscriptFetcher` via the `youtube://` canonical prefix.
- The `content_path` field is used by the render step (via `get_node`): a node with `content_path` pointing to a cache file derives the rendering for that extra content. The renderer already handles nodes with no `content_path` (skips them).
- A future enhancement could make the routing table extensible via `MEMEX_FETCHER_MODULE` for per-platform pluggable extractors. For now, the router is a fixed composition.
- The `content_path` cache location should be consistent with the vault path passed by the CLI (`--vault`), which is always available in `ingest_single_url`.
