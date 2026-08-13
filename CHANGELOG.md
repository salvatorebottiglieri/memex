# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Telegram capture: `memex capture` polls Telegram Saved Messages (Telethon) and appends one inbox row per URL, advancing a per-source cursor (ADR-0006) so re-runs only fetch new messages. Backed by new cursor/inbox store tables (open-time migration).
- Inbox ingest: `memex ingest --from-inbox` runs pending inbox items through the shared extract path; canonical-key dedup makes it idempotent and non-destructive (ADR-0007), with failed fetches left pending for retry.

## [0.1.0] — 2026-08-06

### Added

- Core JSON-only CLI (AXI standard) — init, status, list, show, search, ideas, stats, resolve, cookies-export, render, delete, backfill-synthesis, ontology, sync.
- Versioning: `memex version` (JSON) and `memex --version` — both read from package metadata (importlib.metadata), no hardcoded copy.
- Ingestion: `register` creates a URL-node + extracted-node pair; `extract` fetches URLs via per-type fetchers (web, YouTube transcripts, reference-based document reading).
- Idea extraction (`extract-ideas`) with batch mode and idea search (`ideas`).
- Derivation: notes-tier (`derive`) and synthesis-tier (`synthesize`) via pluggable LLM agents (demo, OMP RPC, Claude Code), with `derive --all` batching.
- Adversarial quality validation of derivations via a separate validator agent (`MEMEX_VALIDATOR`).
- Contested propagation and review workflow: `contradict`, `relate`, `review` accept/reject/dismiss, with staleness propagation.
- Obsidian rendering: `render` projects the SQLite graph into markdown frontmatter and wikilinks (ADR-0008), with vault auto-detection.
- Git sync: `sync` commits vault state and pushes in one shot; post-merge hook auto-renders on pull.
- Resolution rules for arXiv/GitHub/Wikipedia (`resolve`) and browser-assisted agent resolution (`resolve-agent`).
- CI: pytest + ruff on push/PR.
