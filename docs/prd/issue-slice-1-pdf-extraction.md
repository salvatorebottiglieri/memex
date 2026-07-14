## Parent

#41

## What to build

Add PDF text extraction to memex. Introduce a `RoutingFetcher` that dispatches based on canonical key prefixes, a `PDFFetcher` that extracts text via `pypdf`, and add the `content_path` field to `FetchResult` (unused by PDFFetcher but part of the interface).

The routing layer (`RoutingFetcher`) wraps all available fetchers and delegates to the right extractor based on the canonical key prefix. For PDF URLs (any `http[s]://` URL whose path ends in `.pdf`), the `PDFFetcher` extracts text from the downloaded PDF and returns it as the L0 content — same pattern as `HttpFetcher` but with a different backend.

The router is transparent: all existing callers (`ingest_single_url`, `_do_derive`, `_derive_all_inner`) use `fetcher.fetch(url)` with no signature change. The `load_fetcher()` default changes from returning `HttpFetcher()` to returning `RoutingFetcher([HttpFetcher, PDFFetcher])`.

The `FetchResult` dataclass gains an `content_path: str | None = None` field for future use (YouTube cache). PDFFetcher never sets it.

Add `pypdf` as an optional dependency in `pyproject.toml` under a `media` extras group.

## User stories covered

- 8: `memex ingest https://example.com/paper.pdf` produces an L0 node with extracted text
- 9: A PDF whose text extraction fails (encrypted, image-only) produces a `failed` source row, not a crash
- 10: `HttpFetcher` remains the default for non-PDF URLs
- 11: `memex derive` on a PDF L0 produces a notes-tier derivation from extracted text
- 12: Routing is based on canonical key prefix (no new URL parsing)
- 13: No new CLI flags — `memex ingest <url>` just works for PDFs
- 14: `content_path` is opt-in (PDFFetcher never sets it)

## Acceptance criteria

- [ ] `FetchResult` gains `content_path: str | None = None` — no breaking change to existing callers
- [ ] `RoutingFetcher` dispatches `http[s]://*.pdf` URLs to `PDFFetcher`, all others to `HttpFetcher`
- [ ] `load_fetcher()` returns `RoutingFetcher` by default (backward-compatible with `MEMEX_FETCHER_MODULE` override)
- [ ] `PDFFetcher.fetch()` downloads a PDF, extracts text, returns it in `FetchResult.content`
- [ ] `PDFFetcher.fetch()` raises `FetchError` for encrypted/binary/network failures (same pattern as `HttpFetcher`)
- [ ] `ingest_single_url` with a PDF URL produces an L0 node with extracted text as the content file
- [ ] `ingest_single_url` with a PDF that fails extraction produces a `failed` source row — not a crash
- [ ] `pypdf` is an optional dependency (`pip install memex[media]`), lazily imported inside `PDFFetcher.fetch()`
- [ ] `test_ingester.py` has a test exercising the PDF path through `ingest_single_url`
- [ ] `tests/test_fetcher.py` has unit tests for `RoutingFetcher._select()` dispatch, `PDFFetcher.fetch()` with a mocked `pypdf`, and the `FetchResult.content_path` field
- [ ] `smoke_test.py` has a `smoke_fetcher_pdf` test that uses `MEMEX_FETCHER_MODULE` to inject a fake fetcher returning PDF-shaped content — asserts L0 has extracted text and derives from it

## Blocked by

None — can start immediately
