# ADR-0015: Shared vault via git sync

The vault (SQLite DB + markdown files) is colocated in one folder per ADR-0008.
A single-user knowledge base on one machine is the default; this ADR makes it
trivially shareable across devices without adding a daemon, server, or sync protocol.

## Decision

### Git is the sync layer

The vault folder is a **git repo**. Push/pull between devices. No cloud provider,
no daemon, no merge framework. Git was the user's explicit choice.

### DB is committed to git

Per ADR-0008, SQLite is the source of truth for structure (edges, trust state,
confidence, ledger). Leaving the DB out of git would make a fresh clone useless
— no provenance, no metadata, no state. The DB lives in the repo and is committed
alongside markdown files.

The DB is read/written synchronously by `memex` CLI commands (ingest, derive,
synthesize, render). The commit serializes to a single writer — last-writer-wins
on sync. Git LFS is NOT used (SQLite files below 1 MiB in practice; a binary
diff every commit is fine for single-user scale).

### Env vars override auto-detect

`MEMEX_VAULT` and `MEMEX_DB` environment variables let each device point at the
shared repo without relying on the Obsidian auto-detect path (which scans
`~/Obsidian`, `~/notes`, etc. — brittle when the vault lives at a custom path).

Precedence, highest first:
1. `--vault` / `--db` CLI flags (unchanged)
2. `MEMEX_VAULT` / `MEMEX_DB` env vars (new)
3. Obsidian auto-detect via `~/.obsidian` (unchanged)
4. Fallback `~/memex-vault` (unchanged)

### Recommended `.gitignore`

The vault root should contain a `.gitignore` committing these paths (machine-local):
- `.obsidian/` — per-machine UI config (themes, plugins, hotkeys)
- `.cache/` — runtime fetcher caches (YouTube transcripts, PDF text)
- `__pycache__/`, `*.py[cod]` — Python bytecode (if `.venv` ends up inside the vault)

### Workflow discipline

Render before commit on every device so frontmatter matches the DB:

```
# On device A
cd ~/vault
memex render
git add -A
git commit -m "sync"
git push

# On device B  
git pull
memex render  # reconcile frontmatter against the pulled DB
```

Due to last-writer-wins, two devices rendering the same node at the same
commit point produces identical output (renderer is deterministic from DB
state). The only conflict surface is two devices rendering *different* DB
states and the later push overwriting frontmatter from the earlier one —
acceptable for single-user use. If a push is rejected, `git pull --rebase`
resolves.

### Automation via `memex sync`

The manual workflow is automated by a single CLI command (added in this ADR):

```bash
memex sync [--no-push]
```

`memex sync` chains render → `git add -A` → `git commit -m "sync"` → `git push`.
`--push/--no-push` defaults to push. JSON output: `{rendered, committed, pushed}`.

A git post-merge hook can be installed via:

```bash
memex sync --install-hooks
```

This writes `.git/hooks/post-merge` with `exec memex render --vault "<abs-path>"`,
so every `git pull` auto-renders frontmatter against the pulled DB — no manual step.
The hook path is absolute, so it works without `MEMEX_VAULT` being set.

### Remote e credenziali

`memex sync` chiama `git push` sul remote predefinito — chi usa la vault
configura il remote una volta con `git remote add origin <url>`. Non c'è
nessuna logica di remote discovery in memex.

Le credenziali sono interamente gestite da git: SSH keys, credential helper
(libsecret, oauth, wincred), token in `~/.netrc`. `memex sync` non le tocca,
non chiede password, non salva token. Se `git push` fallisce, l'errore di git
viene riportato verbatim nel JSON di errore.

### Capture session state stays out of vault

Telegram capture data (`~/.memex/telegram.session`, inbox cursors in the DB)
is already in the DB. The per-machine Telegram API session file stays at
`~/.memex/` and is **not** synced — each device logs in independently. Inbox
content (raw URLs + timestamps) is in the DB and does sync, so a capture on
device A is visible to `memex ingest --from-inbox` on device B.

## Consequences

**Positive:**
- Zero infra beyond git. Works on any device with git and `memex` installed.
- Existing CLI flags `--db` / `--vault` unchanged; env vars are a backward-compatible addition.
- DB is always consistent with markdown on every clone — no separate restoration step.
- Private per-device state (`.obsidian/`, `.cache/`, `.venv/`) stays out of git.
- ADR-0008's view-only constraint unchanged; `memex render` is the required sync step.

**Negative:**
- Git diff noise on SQLite (binary blob per commit). Acceptable at single-user scale.
- Two devices MUST NOT run `memex` concurrently on the same DB file — no WAL-mode multidevice support. Push/pull discipline is the coordination mechanism.
- `telegram.session` must be re-created on each device (one-time auth). Cursor position in the DB is shared, so re-capture of already-captured messages is idempotent.

**Risks and mitigations:**
- **Binary merge conflict on DB:** Git cannot merge two divergent SQLite files — a rebase picks one binary version whole. If a `git pull --rebase` produces a conflict, accept one side and re-derive lost work. In practice a single user on two devices with disciplined render-before-commit produces linear history, making this a theoretical risk.
