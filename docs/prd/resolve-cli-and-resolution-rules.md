# PRD: `memex resolve` CLI and Resolution Rules

## Problem Statement

I save links from many sources — X/Twitter (with links to articles inside), arXiv (abstract pages I actually want the PDF of), YouTube, GitHub repos, Wikipedia, and plain web articles. When an external agent (Pi, Claude Code, custom) sees one of these URLs, it has no way to know *what kind* of content it is or whether `memex ingest` can handle it directly.

Currently:
- YouTube and PDF URLs work (RoutingFetcher dispatches them correctly)
- arXiv abs pages get scraped as HTML instead of fetched as PDF
- GitHub URLs get scraped as HTML instead of fetched via the raw content API
- Wikipedia pages get scraped as HTML instead of fetched via the REST API
- X/Twitter URLs hit HttpFetcher, produce useless output, and the agent has no way of knowing it should use a browser first
- The external agent has no way to "pre-flight" a URL before calling ingest

## Solution

1. **`memex resolve <url>`** — a new CLI command that classifies a URL deterministically and returns a JSON envelope telling the external agent what to do. No LLM, purely rule-based.

2. **Resolution rules** — a registry of deterministic patterns that extend RoutingFetcher's dispatch and also inform `resolve`. New rules for arXiv, GitHub, and Wikipedia in addition to existing YouTube and PDF handling.

3. **No change to the external-agents-are-clients model** (ADR-0010). Memex provides facilities; the agent orchestrates.

## User Stories

1. As an agent user, I want to run `memex resolve <url>` so that I can know, before calling ingest, whether a URL can be handled directly.

2. As an agent user, I want `memex resolve` to return a JSON object with `type`, `ingestable`, `fetcher`, and `direct_url` fields so that my agent can parse the response programmatically.

3. As an agent user, I want `memex resolve` to recognize arXiv abstract pages (`arxiv.org/abs/XXXX`) and return `ingestable: true` with a `direct_url` pointing to the PDF, so that my agent can call `memex ingest` with the right URL.

4. As an agent user, I want `memex resolve` to recognize GitHub file URLs (`github.com/{owner}/{repo}/blob/{branch}/{path}`) and return `ingestable: true` with a `direct_url` pointing to the raw content, so that my agent can ingest the source file directly.

5. As an agent user, I want `memex resolve` to recognize Wikipedia pages (`*.wikipedia.org/wiki/Title`) and return `ingestable: true` with a `direct_url` pointing to the REST API summary, so that my agent can ingest the article content cleanly.

6. As an agent user, I want `memex resolve` to recognize YouTube URLs and return `ingestable: true` with `fetcher: "YouTubeTranscriptFetcher"`, so that my agent knows the transcript will be captured.

7. As an agent user, I want `memex resolve` to recognize direct PDF URLs and return `ingestable: true` with `fetcher: "PDFFetcher"`, so that my agent knows the PDF will be extracted.

8. As an agent user, I want `memex resolve` to return `ingestable: false` for URLs that RoutingFetcher cannot handle (e.g. X/Twitter, image files, video files), along with a `note` explaining why, so that my agent can fall back to its browser tool.

9. As an agent user, I want `memex resolve` to return `ingestable: true` with `fetcher: "HttpFetcher"` for any plain web article URL, so that my agent knows basic text extraction will work.

10. As a developer, I want resolution rules to be registered in a central registry so that adding a new site (e.g. Reddit, Medium) requires no changes to the CLI command or RoutingFetcher dispatch logic.

11. As a developer, I want `memex resolve` to fail gracefully with a clean JSON error when the URL is malformed or unreachable.

## Implementation Decisions

### 1. New CLI command: `memex resolve <url>`

```
$ memex resolve https://arxiv.org/abs/2304.12345
{"url":"https://arxiv.org/abs/2304.12345","type":"arxiv","ingestable":true,"fetcher":"PDFFetcher","direct_url":"https://arxiv.org/pdf/2304.12345"}

$ memex resolve https://x.com/user/status/123
{"url":"https://x.com/user/status/123","type":"unknown","ingestable":false,"note":"URL non riconosciuto. L'agente esterno può usare il browser per estrarre l'URL target e poi chiamare memex ingest."}

$ memex resolve https://example.com/article
{"url":"https://example.com/article","type":"web","ingestable":true,"fetcher":"HttpFetcher"}

$ memex resolve https://www.youtube.com/watch?v=abc123
{"url":"https://www.youtube.com/watch?v=abc123","type":"youtube","ingestable":true,"fetcher":"YouTubeTranscriptFetcher"}

$ memex resolve https://github.com/user/repo/blob/main/file.py
{"url":"https://github.com/user/repo/blob/main/file.py","type":"github_file","ingestable":true,"fetcher":"HttpFetcher","direct_url":"https://raw.githubusercontent.com/user/repo/main/file.py"}

$ memex resolve https://en.wikipedia.org/wiki/Python_(programming_language)
{"url":"https://en.wikipedia.org/wiki/Python_(programming_language)","type":"wikipedia","ingestable":true,"fetcher":"HttpFetcher","direct_url":"https://en.wikipedia.org/api/rest_v1/page/summary/Python_(programming_language)"}
```

### 2. Resolution rule registry

A new `ResolutionRule` protocol/registry separate from `RoutingFetcher._select()`, shared between `resolve` and `ingest`:

```
ResolutionRule.match(url) -> Resolution | None
Resolution { type: str, fetcher: str, direct_url: str | None, ingestable: bool, note: str | None }
```

Rules iterate in priority order. First match wins.

Initial rules:
- **arXivRule**: matches `arxiv.org/abs/` → strips `/abs/` to `/pdf/`
- **GitHubBlobRule**: matches `github.com/*/blob/` → rewrites to `raw.githubusercontent.com`
- **WikipediaRule**: matches `*.wikipedia.org/wiki/` → rewrites to REST API `/api/rest_v1/page/summary/`
- **YouTubeRule**: matches canonical key `youtube://` prefix (already in canonical_key.py)
- **PdfRule**: matches `.pdf` suffix (already in RoutingFetcher._select)
- **DefaultRule**: matches any `http://` or `https://` → HttpFetcher, ingestable

### 3. Integration with RoutingFetcher

`RoutingFetcher._select()` gains awareness of resolution rules that transform URLs before dispatch. For arXiv: the rule rewrites `abs/` → `pdf/` before the `.pdf` check catches it, so `PDFFetcher` is selected.

This means `ingest` also benefits: `memex ingest https://arxiv.org/abs/2304.12345` automatically fetches the PDF.

### 4. Non-ingestable types

URLs that resolve to non-text content (images, videos, binaries) or JS-required pages (X/Twitter, Reddit) return `ingestable: false`. The agent decides what to do — typically opening a browser.

### 5. Canonical key as input

Both `resolve` and `_select` use the already-computed `canonical_key(url)` as the primary input for matching, not the raw URL. The resolution rules operate on the raw URL for pattern matching but the canonical key for type identification.

### 6. Output envelope

```python
@dataclass
class Resolution:
    url: str            # original URL
    type: str           # classification: "arxiv", "youtube", "pdf", "github_file", "wikipedia", "web", "unknown"
    ingestable: bool    # can memex ingest this directly?
    fetcher: str | None # suggested fetcher name (only when ingestable)
    direct_url: str | None # transformed URL for ingest (when applicable)
    note: str | None    # guidance for the agent (when not ingestable)
```

## Testing Decisions

### Test philosophy

Test external behavior, not implementation details. A good test:
- Provides a URL and asserts the resolution output envelope fields
- Does not depend on internal rule ordering beyond what's documented
- Uses the same seam as real usage (CLI subprocess or `RoutingFetcher._select()`)

### Modules to test

1. **`test_fetcher.py`** (existing file) — add test class `TestResolutionRules` alongside the existing `TestRoutingFetcherSelect`. Tests the rule registry directly: for each rule, assert match/no-match and the returned `Resolution` fields.

2. **New `test_resolve.py`** — integration tests for `memex resolve <url>` as a CLI subprocess. Uses the existing `_run_memex` helper from `conftest.py` with `MEMEX_FETCHER_MODULE`. Tests the JSON output envelope for each URL type.

### Prior art

- `test_fetcher.py:TestRoutingFetcherSelect` — directly tests `_select()` with various URL patterns, asserts correct fetcher class is returned. The resolution rule tests follow the same pattern.
- `test_ingest.py` — tests CLI subprocess with JSON output parsing. The resolve CLI tests follow the same pattern.

### Seam strategy

- **Highest seam**: `ResolutionRule.match(url)` — unit test each rule in isolation. One class per rule, one test per match/non-match case.
- **Integration seam**: `memex resolve <url>` as subprocess — tests the full pipeline from CLI argument through rule registry to JSON output.
- No new seam needed: the existing `MEMEX_FETCHER_MODULE` injection works because resolve is purely URL-classification, not fetch.

## Out of Scope

- **LLM-based resolution**: No LLM inside memex. The external agent is the resolver for complex cases (X/Twitter, JS pages).
- **Browser automation**: memex will not include a headless browser. That belongs to the external agent.
- **Media file ingestion**: Images, video, audio files are not ingestable as text. `resolve` returns `ingestable: false`.
- **Caching resolved URLs**: No cache layer for resolution results. Each `resolve` call is fresh.
- **New fetcher implementations for GitHub/Wikipedia**: The raw content API URLs are returned as `direct_url`; the agent or `ingest` uses existing `HttpFetcher` to fetch them. No new `GitHubRawFetcher` or `WikipediaRestFetcher` classes.

## Further Notes

- The resolution rule registry is designed to be extended by adding a new class. No changes to CLI or RoutingFetcher needed for new rules.
- arXiv resolution is the highest priority: the user regularly saves arXiv links and currently gets HTML instead of the paper.
- `resolve` is also useful for debugging: run `memex resolve <url>` to understand what memex thinks a URL is before deciding whether to ingest.
