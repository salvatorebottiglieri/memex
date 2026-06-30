# Two-store architecture: SQLite owns structure, markdown owns content

**SQLite is the source of truth for structure** — provenance + association edges, tier, trust state, computed depth, confidence, ledger, cursors. **Markdown files are the source of truth for content** — L0 raw and derivation prose. The two are joined by a stable node id. Frontmatter mirrors relations so Obsidian's graph works, but SQLite is authoritative for those fields; a **render step** projects edges to `[[wikilinks]]` one-way (DB → markdown), keeping Obsidian a **view-only** surface.

## Considered Options

- **Pure markdown + frontmatter** (the `ai-research-os` reference) — rejected: traversal, staleness propagation, and structured queries force the agent to read files (token cost) and do not scale to years of links.
- **Full database, no markdown** — rejected: loses human-editable, git-diffable, Obsidian-browsable content and the round-trip for human review.
- **Two stores, clean ownership** (chosen).

## Consequences

Accepted cost: two stores must be kept in sync by disciplined regeneration/render (drift risk if skipped). Human prose edits are authoritative in markdown; state transitions are authoritative in SQLite — neither overwrites the other. Obsidian's global graph is a hairball at scale; its value is the local graph (associations) + Breadcrumbs (provenance hierarchy), not the global view.
