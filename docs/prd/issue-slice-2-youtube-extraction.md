## Parent

#41

## What to build

Add YouTube transcript extraction to memex. Implement `YouTubeTranscriptFetcher` that fetches transcript text via `youtube-transcript-api`, writes it to a cache file in `$VAULT/.cache/`, and returns metadata-only (title, channel) as the L0 content with `content_path` pointing to the cached transcript.

The `RoutingFetcher` (introduced in slice 1) gains a `youtube://` -> `YouTubeTranscriptFetcher` entry in its routing table.

Key behaviors:
- **Transcript available**: writes to `$VAULT/.cache/youtube-<id>.md`, returns metadata in `content`, `content_path` set to cache file path
- **Transcript disabled/unavailable**: returns metadata in `content`, `content_path = None` — the L0 is metadata-only
- **Network/rate limiting**: raises `FetchError` (infrastructure failure, same pattern as all fetchers)
- **Cache is immutable once written**: re-derive reuses the cache file. Manual delete forces re-fetch.
- **Derive reads from `content_path`**: when `content_path` is set, the deriver reads the transcript from the cache file for the agent
- **Derive on no-transcript L0**: graceful failure (not a crash) — the L0 exists but has no substantive content for derivation

Add `youtube-transcript-api` as an optional dependency under the `media` extras group.

## User stories covered

- 1: `memex ingest https://youtube.com/watch?v=ABC123` produces an L0 with title + channel metadata
- 2: YouTube transcript is fetched and cached when available — agent can derive from it
- 3: Video with no transcript still ingested as metadata-only L0 — no crash, no data loss
- 4: `memex derive` on a YouTube L0 works when transcript exists; graceful failure when it doesn't
- 5: Re-ingest hits dedup -> `already_exists`, transcript not re-fetched
- 6: Cached transcript is immutable once written — re-derive reuses it
- 7: Delete `$VAULT/.cache/youtube-<id>.md` manually to force fresh fetch
- 12: Routing uses existing canonical key prefix (`youtube://`)
- 13: No new CLI flags — `memex ingest <url>` just works
- 14: `content_path` is opt-in (set only by YouTubeTranscriptFetcher)

## Acceptance criteria

- [ ] `YouTubeTranscriptFetcher` registers in `RoutingFetcher` under `youtube://` prefix
- [ ] `YouTubeTranscriptFetcher.fetch()` with transcript available writes `$VAULT/.cache/youtube-<id>.md`, returns metadata in `content`, sets `content_path`
- [ ] `YouTubeTranscriptFetcher.fetch()` with transcript disabled/unavailable returns metadata in `content`, `content_path = None` — no cache file written
- [ ] `YouTubeTranscriptFetcher.fetch()` raises `FetchError` on network/rate-limiting failures
- [ ] The deriver (`_do_derive`) reads `node.content_path` when set, falling back to L0 markdown file path — YouTube derives use the cached transcript as their source
- [ ] Derive on a metadata-only YouTube L0 (no transcript, no L0 markdown file) produces a graceful error — not a crash
- [ ] `youtube-transcript-api` is optional (`pip install memex[media]`), lazily imported inside `YouTubeTranscriptFetcher.fetch()`
- [ ] `tests/test_fetcher.py` has unit tests for `YouTubeTranscriptFetcher` with a mocked `youtube-transcript-api`, covering all three outcomes (available, unavailable, network error)
- [ ] `test_ingester.py` has a test exercising the YouTube path through `ingest_single_url` — asserts metadata-only L0 + content_path
- [ ] `smoke_test.py` has a `smoke_fetcher_youtube` test that uses `MEMEX_FETCHER_MODULE` to inject a fake fetcher returning YouTube-shaped content — asserts L0 has metadata content and derives from content_path

## Blocked by

None — can start immediately. This slice registers into the `RoutingFetcher` routing table alongside PDF extraction (no dependency on PDF implementation).
