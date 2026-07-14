# ADR-0013: Fetcher router with per-type extractors

Content extraction from URLs is currently monolithic: `HttpFetcher` downloads any URL as HTML and does a regex strip-tag to markdown. This works for articles but fails for YouTube (HTML contains no transcript) and PDFs (binary, not HTML). We need a routing layer that dispatches URLs to the right extractor.

## Decision

### Fetcher router

A `RoutingFetcher` wraps all available fetchers and dispatches based on the **canonical key** prefix (already computed by `memex.canonical_key.canonical_key()`):

```
RoutingFetcher.fetch(url):
    ckey = canonical_key(url)
    fetcher = self._select(ckey)
    return fetcher.fetch(url)
```

`_select` maps `ckey` prefixes to fetcher classes:

| ckey prefix | Fetcher |
|---|---|
| `youtube://` | `YouTubeTranscriptFetcher` |
| `http://` / `https://` -> URL ends with `.pdf` | `PDFFetcher` |
| `http://` / `https://` (default) | `HttpFetcher` |

The router is composed at construction time from a `{prefix: FetcherClass}` dict. The interface remains `ContentFetcher.fetch(url) -> FetchResult` — all callers (`ingest_single_url`, `_derive_single`) are unchanged.

### FetchResult now includes `content_path`

```python
@dataclass
class FetchResult:
    content: str
    content_path: str | None = None  # new
    title: str | None = None
```

For media sources whose L0 is metadata-only (YouTube), `content_path` points to a **cached transcript** in `$VAULT/.cache/`. The agent receives both `content` and `content_path` and may choose to read the file incrementally.

`content_path` is **opt-in**: `HttpFetcher` and `PDFFetcher` never set it. Only fetchers that produce an artifact external to the L0 markdown file (transcripts, audio -> text) set this field.

### Cache location

Per-type artifacts live in `$VAULT/.cache/` with the naming convention `<type>-<canonical-id>.md`:

- YouTube transcript: `$VAULT/.cache/youtube-<id>.md`
- (Future) audio transcription: `$VAULT/.cache/audio-<hash>.md`

This cache is **immutable once written**: re-derive reuses the cached artifact. To force a refresh, delete the cache file manually.

### Per-type extractor design

| Extractor | L0 content | content_path | Dependency |
|---|---|---|---|
| `HttpFetcher` | Extracted markdown | None | stdlib only |
| `YouTubeTranscriptFetcher` | Metadata (title, channel) | Path to cached transcript | `youtube-transcript-api` |
| `PDFFetcher` | Extracted text | None | `pypdf` |

### YouTube error handling

`YouTubeTranscriptFetcher.fetch()` never raises `FetchError` for content issues:

- **Transcript available** -> writes cache, returns metadata in `content`, `content_path` set
- **Transcript disabled / unavailable** -> returns metadata in `content`, `content_path = None`
- **Network / rate limiting** -> raises `FetchError` (same as any fetcher)

The derive caller checks `content_path`: if None and the node has no markdown file, derive fails gracefully. This keeps the fetcher interface clean: `FetchError` only for infrastructure failures, never for expected content absences.

### Extensibility

New extractors add a canonical key prefix -> fetcher class mapping to `RoutingFetcher._select()`. No changes to the caller chain.

## Consequences

- **Positive**: Single code path for all ingest and derive — `fetcher.fetch(url)`. Router is transparent.
- **Positive**: YouTube extraction doesn't leak into L0 model. L0 stays as metadata, transcript is a derived cache artifact.
- **Positive**: PDF extraction is a straight drop-in — same pattern as HTML, just a different backend.
- **Negative**: Cache in `$VAULT/.cache/` is a new directory with no GC policy. Manual cleanup for now.
- **Risk**: YouTube transcript fetch adds dependencies. If `youtube-transcript-api` breaks, cached nodes are fine, uncached ones can't be derived until fixed.
