# AGENTS.md

Guidance for AI agents working in this repo. memex is harness-agnostic by design (see
`docs/adr/0010-cli-canonical-interface-no-mcp.md`) — these notes apply to any agent/harness.

Start by reading `CONTEXT.md` (glossary) and `docs/ARCHITECTURE.md` (vision + ADR index).
Respect the decisions in `docs/adr/`; if your work contradicts one, surface it rather than
silently overriding it.

## Agent skills

### Issue tracker

Issues and PRDs live as **GitHub Issues** on `salvatorebottiglieri/memex`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles use their default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Releasing

Versioning is semver (`MAJOR.MINOR.PATCH`): `MAJOR` for breaking CLI changes, `MINOR` for new features, `PATCH` for fixes. The version lives only in `pyproject.toml`; `memex version` / `memex --version` read it via installed package metadata (`importlib.metadata`) — never duplicate it.

To release:

1. Move the `CHANGELOG.md` entries from `[Unreleased]` into a new `[X.Y.Z] — <date>` section; bump `version` in `pyproject.toml`.
2. Open a PR and merge after CI passes (`uv run pytest tests/`, `uv run ruff check src tests`).
3. Tag the merged commit and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
4. Done — nothing else to sync; the CLI reads the version from package metadata.
