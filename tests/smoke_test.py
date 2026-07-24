"""Aggressive end-to-end smoke tests for memex.

Exercises every CLI command via real subprocess against a temp vault/db.
Each test is a small sequence of CLI invocations + assertions on stdout/exit.

This is *not* a pytest module — invoke directly:

    uv run python tests/smoke_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_FAILING_AGENT = "tests.fake_llm_client_failing:FakeLLMClientFailing"


# ── helpers ──────────────────────────────────────────────────────


class SmokeError(AssertionError):
    pass


_failures: list[str] = []
_passes = 0


def _run(args: list[str], env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    if shutil.which("uv"):
        cmd = ["uv", "run", "python", "-m", "memex.cli", *args]
    else:
        cmd = [sys.executable, "-m", "memex.cli", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=str(cwd) if cwd else str(REPO),
        timeout=30,
    )


def _check(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  ✓ {name}")
    else:
        _failures.append(f"{name}: {detail}")
        print(f"  ✗ {name}  {detail}")


def _expect_json(name: str, proc: subprocess.CompletedProcess) -> dict | list:
    if proc.returncode != 0:
        raise SmokeError(f"{name} exit={proc.returncode} stderr={proc.stderr!r}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise SmokeError(f"{name} invalid JSON: stdout={proc.stdout!r}") from e


def _fresh_store(tmp: Path, name: str = "smoke") -> tuple[Path, Path]:
    """Return (db_path, vault_path) inside tmp, initialised via `memex init`."""
    db = tmp / f"{name}.db"
    vault = tmp / f"{name}_vault"
    proc = _run(["init", "--db", str(db), "--vault", str(vault)])
    _expect_json(f"init {name}", proc)
    return db, vault


def _node_file(db: Path, node_id: str) -> Path:
    """Return the filesystem path to a node's content, reading from the DB."""
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT content_path FROM node WHERE id = ?", (node_id,)).fetchone()
    con.close()
    if row is None or not row[0]:
        raise SmokeError(f"node {node_id}: no content_path in DB")
    return Path(row[0])


def _register_file(
    vault: Path, filename: str, source_url: str,
    body: str | None = None,
) -> Path:
    """Write a markdown file with frontmatter in the vault and return its path.

    The body defaults to content exceeding 100 characters so that the L0
    markdown file gets created with a content_path in the DB.
    """
    if body is None:
        body = (
            f"# Fake Article\n\n"
            f"Fake content for {source_url}.\n\n"
            f"This is a longer article body that exceeds the minimum character threshold "
            f"of one hundred characters so that the L0 markdown file gets created in tests."
        )
    content = (
        f"---\n"
        f"source_url: {source_url}\n"
        f"title: Fake Article Title\n"
        f"---\n\n"
        f"{body}"
    )
    path = vault / filename
    path.write_text(content, encoding="utf-8")
    return path


# ── smoke groups ─────────────────────────────────────────────────


def smoke_lifecycle(tmp: Path) -> None:
    print("\n[LIFECYCLE] init -> status -> register -> list -> show")
    db, vault = _fresh_store(tmp, "lifecycle")
    s = _run(["status", "--db", str(db), "--vault", str(vault)])
    d = _expect_json("status after init", s)
    _check("status reports exists=true after init", d["db_exists"] and d["vault_exists"])

    md_path = _register_file(vault, "lifecycle.md", "https://example.com/article")
    proc = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    d = _expect_json("register URL", proc)
    _check("register returns node_id", "id" in d)
    _check("register status=registered", d.get("status") == "registered")
    node_id = d["id"]

    # L0 file actually exists on disk (the same file we wrote)
    md_files = list(vault.glob("*.md"))
    _check("vault has 1 md file", len(md_files) == 1, f"got {len(md_files)}")

    # L0 file content matches what we wrote
    content = md_files[0].read_text()
    _check("L0 file contains registered content", "Fake Article" in content)

    proc = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list", proc)
    _check("list returns 1 node", len(lst) == 1, f"got {len(lst)}")
    _check("list node has expected fields",
           set(lst[0].keys()) >= {"id", "kind", "tier", "trust_state", "canonical_key"})
    _check("list node kind=raw_source", lst[0]["kind"] == "raw_source")
    _check("list node trust_state=draft", lst[0]["trust_state"] == "draft")
    _check("list node canonical_key matches source_url",
           lst[0]["canonical_key"] == "https://example.com/article")

    proc = _run(["show", "--db", str(db), "--vault", str(vault), node_id])
    sh = _expect_json("show", proc)
    _check("show returns the node", sh["id"] == node_id)
    _check("show includes content", sh.get("content") is not None)
    _check("show check_failures is null for L0", sh.get("check_failures") is None)
    _check("show l0_path set", sh.get("l0_path") is not None)


def smoke_idempotency(tmp: Path) -> None:
    print("\n[IDEMPOTENCY] register same source_url twice -> already_exists")
    db, vault = _fresh_store(tmp, "idem")
    md_path = _register_file(vault, "idem.md", "https://example.com/article?utm_source=twitter")

    p1 = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    d1 = _expect_json("register #1", p1)
    p2 = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    d2 = _expect_json("register #2", p2)
    _check("same id returned", d1["id"] == d2["id"])
    _check("second status=already_exists", d2.get("status") == "already_exists")

    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    s = con.execute("SELECT COUNT(*) FROM source").fetchone()[0]
    con.close()
    _check("exactly 1 node row", n == 1, f"got {n}")
    _check("exactly 1 source row", s == 1, f"got {s}")


def smoke_derive_passing(tmp: Path) -> None:
    print("\n[DERIVE PASS] derive -> auto-verified")
    db, vault = _fresh_store(tmp, "deriveok")
    md_path = _register_file(vault, "deriveok.md", "https://example.com/article")
    p = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    l0_id = _expect_json("register for derive", p)["id"]

    p = _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_AGENT": FAKE_AGENT})
    d = _expect_json("derive", p)
    _check("derive status=derived", d.get("status") == "derived")
    _check("trust_state=auto-verified", d.get("trust_state") == "auto-verified")
    _check("check_failures empty", d.get("check_failures") == [])
    deriv_id = d["id"]

    # show surfaces trust_state + check_failures
    p = _run(["show", "--db", str(db), "--vault", str(vault), deriv_id])
    sh = _expect_json("show derivation", p)
    _check("show trust_state=auto-verified", sh["trust_state"] == "auto-verified")
    _check("show check_failures=[]", sh["check_failures"] == [])

    # Edges in DB
    con = sqlite3.connect(db)
    edge = con.execute(
        "SELECT type, relation, from_node, to_node FROM edge WHERE from_node = ?",
        (deriv_id,),
    ).fetchone()
    con.close()
    _check("provenance edge exists", edge is not None and edge[0] == "provenance"
           and edge[1] == "derived_from" and edge[3] == l0_id,
           f"got {edge}")

    # list includes the derivation
    p = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list with derivation", p)
    _check("list has 2 nodes (l0 + derivation)", len(lst) == 2, f"got {len(lst)}")

    # Derivation markdown exists and has synthesis marker
    deriv_path = _node_file(db, deriv_id)
    _check("derivation md exists", deriv_path.exists(), f"expected {deriv_path}")
    _check("derivation has > Synthesis: marker", "> Synthesis:" in deriv_path.read_text())


def smoke_derive_failing(tmp: Path) -> None:
    print("\n[DERIVE FAIL] failing LLM -> draft with check_failures")
    db, vault = _fresh_store(tmp, "derivefail")
    md_path = _register_file(vault, "derivefail.md", "https://example.com/article")
    p = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    l0_id = _expect_json("register for failing derive", p)["id"]

    p = _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_AGENT": FAKE_FAILING_AGENT})
    d = _expect_json("derive failing", p)
    _check("derive returns trust_state=draft", d.get("trust_state") == "draft")
    _check("check_failures populated", len(d.get("check_failures", [])) >= 1)

    # DB: trust_state=draft, check_failures column not null
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT trust_state, check_failures FROM node WHERE id = ?", (d["id"],)
    ).fetchone()
    con.close()
    _check("db trust_state=draft", row[0] == "draft")
    _check("db check_failures is non-null JSON", row[1] is not None and len(json.loads(row[1])) >= 1)


def smoke_derive_idempotent(tmp: Path) -> None:
    print("\n[DERIVE IDEMPOTENT] derive twice -> already_derived")
    db, vault = _fresh_store(tmp, "deriveidem")
    md_path = _register_file(vault, "deriveidem.md", "https://example.com/article")
    p = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    l0_id = _expect_json("register", p)["id"]

    _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
         env={"MEMEX_AGENT": FAKE_AGENT})
    p = _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_AGENT": FAKE_AGENT})
    d = _expect_json("derive #2", p)
    _check("second derive status=already_derived", d.get("status") == "already_derived")

    con = sqlite3.connect(db)
    summary_count = con.execute(
        "SELECT COUNT(*) FROM node WHERE kind = 'summary' AND tier = 'notes'"
    ).fetchone()[0]
    edge_count = con.execute(
        "SELECT COUNT(*) FROM edge WHERE to_node = ? AND type = 'provenance'",
        (l0_id,),
    ).fetchone()[0]
    con.close()
    _check("exactly 1 derivation node", summary_count == 1, f"got {summary_count}")
    _check("exactly 1 provenance edge", edge_count == 1, f"got {edge_count}")


def smoke_derive_all(tmp: Path) -> None:
    print("\n[DERIVE ALL] derive --all batch mode")
    db, vault = _fresh_store(tmp, "deriveall")
    env = {"MEMEX_AGENT": FAKE_AGENT}

    # Register 3 files
    for i in range(3):
        md_path = _register_file(vault, f"deriveall-{i}.md", f"https://example.com/article-{i}")
        p = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
        _expect_json(f"register {i}", p)

    p = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list", p)
    l0_ids = [r["id"] for r in lst]
    _check("3 L0 nodes", len(l0_ids) == 3)

    p = _run(["derive", "--db", str(db), "--vault", str(vault), l0_ids[0]], env=env)
    d = _expect_json("manual derive", p)
    _check("manual derive ok", d["status"] == "derived")

    # derive --all with limit 1: 1 already_derived + 1 new
    p = _run(["derive", "--db", str(db), "--vault", str(vault), "--all", "--limit", "1"], env=env)
    res = _expect_json("derive --all limit=1", p)
    _check("2 results (1 already + 1 new)", len(res) == 2, f"got {len(res)}")
    statuses = sorted(r["status"] for r in res)
    _check("statuses: already_derived + derived", statuses == ["already_derived", "derived"])

    # derive --all again: remaining 1
    p = _run(["derive", "--db", str(db), "--vault", str(vault), "--all"], env=env)
    res = _expect_json("derive --all #2", p)
    _check("3 results (2 already + 1 new)", len(res) == 3, f"got {len(res)}")
    derived_count = sum(1 for r in res if r["status"] == "derived")
    already_count = sum(1 for r in res if r["status"] == "already_derived")
    _check("1 new derived", derived_count == 1)
    _check("2 already_derived", already_count == 2)

    # All derived now
    p = _run(["derive", "--db", str(db), "--vault", str(vault), "--all"], env=env)
    res = _expect_json("derive --all #3", p)
    _check("3 already_derived", len(res) == 3 and all(r["status"] == "already_derived" for r in res))


def smoke_search(tmp: Path) -> None:
    print("\n[SEARCH] keyword search over derivations")
    db, vault = _fresh_store(tmp, "search")
    md_path = _register_file(vault, "search.md", "https://example.com/article")
    p = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    l0_id = _expect_json("register for search", p)["id"]
    _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
         env={"MEMEX_AGENT": FAKE_AGENT})

    p = _run(["search", "--db", str(db), "--vault", str(vault), "broader pattern"])
    res = _expect_json("search", p)
    _check("search returns >=1 result", len(res) >= 1, f"got {res}")
    _check("result has required fields",
           all({"id", "snippet", "canonical_key", "l0_node_id"} <= set(r) for r in res))
    _check("snippet contains query", any("broader pattern" in r["snippet"].lower() for r in res))
    _check("l0_node_id points to source", res[0]["l0_node_id"] == l0_id)

    # No match
    p = _run(["search", "--db", str(db), "--vault", str(vault), "xyznonexistentterm"])
    res = _expect_json("search no-match", p)
    _check("no-match returns []", res == [])

    # search is read-only (no new rows)
    con = sqlite3.connect(db)
    n_before = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    e_before = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    con.close()
    _run(["search", "--db", str(db), "--vault", str(vault), "broader pattern"])
    con = sqlite3.connect(db)
    n_after = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    e_after = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    con.close()
    _check("search doesn't write nodes", n_before == n_after)
    _check("search doesn't write edges", e_before == e_after)


def smoke_errors(tmp: Path) -> None:
    print("\n[ERRORS] invalid inputs and unknown ids")
    db, vault = _fresh_store(tmp, "errors")

    # show on unknown id
    p = _run(["show", "--db", str(db), "--vault", str(vault), "does-not-exist"])
    _check("show unknown id exits non-zero", p.returncode != 0)
    out = json.loads(p.stderr)
    _check("show unknown id error=not_found", out.get("error") == "not_found")

    # derive on unknown id
    p = _run(["derive", "--db", str(db), "--vault", str(vault), "nope"],
             env={"MEMEX_AGENT": FAKE_AGENT})
    _check("derive unknown id exits non-zero", p.returncode != 0)

    # delete on unknown id
    p = _run(["delete", "--db", str(db), "--vault", str(vault), "does-not-exist"])
    _check("delete unknown id exits non-zero", p.returncode != 0)

    # Missing arguments
    p = _run(["show", "--db", str(db), "--vault", str(vault)])
    _check("show with no id exits non-zero", p.returncode != 0)


def smoke_migration(tmp: Path) -> None:
    print("\n[MIGRATION] old-schema DB (no check_failures, no failed) -> init -> use")
    db, vault = tmp / "old.db", tmp / "old_vault"
    con = sqlite3.connect(db)
    con.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS node (
            id           TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            tier         TEXT,
            trust_state  TEXT NOT NULL,
            depth        INTEGER NOT NULL,
            content_path TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source (
            node_id       TEXT PRIMARY KEY REFERENCES node(id),
            canonical_key TEXT NOT NULL UNIQUE,
            source_url    TEXT NOT NULL,
            title         TEXT,
            fetched_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS edge (
            id        TEXT PRIMARY KEY,
            type      TEXT NOT NULL,
            relation  TEXT NOT NULL,
            from_node TEXT NOT NULL REFERENCES node(id),
            to_node   TEXT NOT NULL REFERENCES node(id)
        );
    """)
    con.commit()
    con.close()
    vault.mkdir(parents=True, exist_ok=True)

    # init must not crash, must add check_failures + failed columns
    p = _run(["init", "--db", str(db), "--vault", str(vault)])
    _expect_json("init on old schema", p)

    con = sqlite3.connect(db)
    node_cols = {r[1] for r in con.execute("PRAGMA table_info(node)").fetchall()}
    source_cols = {r[1] for r in con.execute("PRAGMA table_info(source)").fetchall()}
    con.close()
    _check("check_failures column added", "check_failures" in node_cols, f"got {node_cols}")
    _check("failed column added to source", "failed" in source_cols, f"got {source_cols}")

    # And register still works on the migrated DB
    md_path = _register_file(vault, "migration.md", "https://example.com/article")
    p = _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])
    d = _expect_json("register after migration", p)
    _check("register works after migration", d.get("status") == "registered")


def smoke_render(tmp: Path) -> None:
    print("\n[RENDER] core render — metadata + tags + aliases")
    db, vault = _fresh_store(tmp, "render")

    # L0 render
    md_path = _register_file(vault, "render.md", "https://example.com/article")
    _run(["register", "--db", str(db), "--vault", str(vault), str(md_path)])

    p = _run(["render", "--db", str(db), "--vault", str(vault)])
    res = _expect_json("render L0", p)
    _check("render returns 1 result", len(res) == 1, f"got {len(res)}")
    _check("render status=rendered", res[0]["status"] == "rendered")

    # Frontmatter is valid YAML with expected fields
    md_files = list(vault.glob("*.md"))
    _check("1 rendered file", len(md_files) == 1)
    import yaml
    text = md_files[0].read_text(encoding="utf-8")
    _check("file starts with ---", text.startswith("---\n"), f"got: {text[:20]!r}")
    _, fm_raw, _ = text.split("---\n", 2)
    fm = yaml.safe_load(fm_raw)
    _check("frontmatter has id", "id" in fm)
    _check("frontmatter has kind=raw_source", fm.get("kind") == "raw_source")
    _check("frontmatter has depth=0", fm.get("depth") == 0)
    _check("frontmatter has tags with kind/raw_source", "kind/raw_source" in fm.get("tags", []))
    _check("frontmatter has source_url", "source_url" in fm)
    _check("frontmatter has title", fm.get("title") == "Fake Article Title")
    _check("frontmatter has aliases", fm.get("aliases") == ["Fake Article Title"])

    # Idempotency
    p = _run(["render", "--db", str(db), "--vault", str(vault)])
    res = _expect_json("render idempotent", p)
    _check("re-render still returns rendered", res[0]["status"] == "rendered")
    import yaml as _y
    text2 = md_files[0].read_text(encoding="utf-8")
    _, fm_raw2, _ = text2.split("---\n", 2)
    fm2 = _y.safe_load(fm_raw2)
    _check("idempotent: same kind", fm2.get("kind") == "raw_source")
    _check("idempotent: same title", fm2.get("title") == "Fake Article Title")

    # Derivation render
    p = _run(["derive", "--db", str(db), "--vault", str(vault), str(res[0]["node_id"])],
             env={"MEMEX_AGENT": FAKE_AGENT})
    d = _expect_json("derive for render", p)
    deriv_id = d["id"]

    p = _run(["render", "--db", str(db), "--vault", str(vault)])
    res = _expect_json("render with derivations", p)
    _check("2 nodes rendered", len(res) == 2, f"got {len(res)}")

    deriv_md = _node_file(db, deriv_id)
    dtext = deriv_md.read_text(encoding="utf-8")
    _, dfm_raw, _ = dtext.split("---\n", 2)
    dfm = _y.safe_load(dfm_raw)
    _check("derivation frontmatter has kind=summary", dfm.get("kind") == "summary")
    _check("derivation frontmatter has tier=notes", dfm.get("tier") == "notes")
    _check("derivation frontmatter has trust_state", dfm.get("trust_state") in ("draft", "auto-verified"))
    _check("derivation tags include kind/summary", "kind/summary" in dfm.get("tags", []))
    _check("derivation tags include tier/notes", "tier/notes" in dfm.get("tags", []))
    _check("derivation frontmatter has synthesis_statements", isinstance(dfm.get("synthesis_statements"), list))
    _check("derivation frontmatter has derived_from", "derived_from" in dfm)
    _check("derived_from is formatted as wikilink", dfm["derived_from"].startswith("[["), f"got {dfm['derived_from']!r}")
    _check("derived_from wikilink is relative path (no full path)", "/" not in dfm["derived_from"].strip("[]").split("|")[0],
           f"got {dfm['derived_from']!r}")
    p = _run(["render", "--db", str(tmp / "nonexistent.db"), "--vault", str(vault)])
    _check("render missing DB exits non-zero", p.returncode != 0)
    db2, vault2 = _fresh_store(tmp, "render-empty")
    p = _run(["render", "--db", str(db2), "--vault", str(vault2)])
    res_empty = _expect_json("render empty", p)
    _check("empty vault returns []", res_empty == [])

    # Missing DB exits error
    p = _run(["render", "--db", str(tmp / "nope.db"), "--vault", str(vault2)])
    _check("render missing DB exits non-zero", p.returncode != 0)



def smoke_help(tmp: Path) -> None:
    print("\n[HELP] every command has --help")
    for cmd in ["init", "status", "register", "list", "show", "derive", "search", "render", "review", "contradict", "resolve"]:
        p = _run([cmd, "--help"])
        _check(f"{cmd} --help exits 0", p.returncode == 0)
        _check(f"{cmd} --help mentions usage", "Usage:" in p.stdout, f"got: {p.stdout[:80]}")


def smoke_full_e2e(tmp: Path) -> None:
    """One continuous flow: register -> derive -> search."""
    print("\n[E2E FLOW] register -> derive -> search -> verify")
    db, vault = _fresh_store(tmp, "e2e")

    # Register two files
    md1 = _register_file(vault, "alpha.md", "https://example.com/alpha",
                         body="# Alpha\n\nBody for alpha node. " + "x" * 120)
    md2 = _register_file(vault, "beta.md", "https://example.com/beta",
                         body="# Beta\n\nBody for beta node. " + "x" * 120)
    _run(["register", "--db", str(db), "--vault", str(vault), str(md1)])
    _run(["register", "--db", str(db), "--vault", str(vault), str(md2)])

    p = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list", p)
    l0_ids = [r["id"] for r in lst]
    _check("2 L0 nodes from register", len(l0_ids) == 2, f"got {len(l0_ids)}")

    for l0_id in l0_ids:
        _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_AGENT": FAKE_AGENT})

    p = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list", p)
    _check("4 nodes total (2 l0 + 2 derivations)", len(lst) == 4, f"got {len(lst)}")

    p = _run(["search", "--db", str(db), "--vault", str(vault), "broader pattern"])
    res = _expect_json("search", p)
    _check("search finds both derivations", len(res) == 2, f"got {len(res)}")


def smoke_review(tmp: Path) -> None:
    """End-to-end review workflow: contradicts edge -> propose -> accept/reject."""
    print("\n[REVIEW] full review workflow — contradicts, propose, accept, verify")

    # ── helpers ────────────────────────────────────────────────
    def _make_fake_review(path: Path, affected_ids: list[str], conf: str = "high") -> str:
        """Write a temp FakeAgent that returns given affected_node_ids.
        Returns the MEMEX_AGENT value (module:Class string)."""
        stem = path.stem
        path.write_text(
            "from tests.fake_llm_client import FakeAgent\n"
            f"class {stem}(FakeAgent):\n"
            "    def __init__(self):\n"
            f"        super().__init__(review_affected_node_ids={affected_ids!r}, review_confidence={conf!r})\n"
        )
        return f"{stem}:{stem}"

    def _create_contradiction(db: Path, deriv_id: str, l0_id: str) -> str:
        """Insert a contradicts edge + event_queue + event_node_link rows.
        Returns the edge_id."""
        import datetime as _dt
        con = sqlite3.connect(str(db))
        con.execute("PRAGMA foreign_keys = ON")
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        edge_id = "e-smoke-" + str(uuid.uuid4())[:8]
        # Edge
        con.execute(
            "INSERT INTO edge (id, type, relation, from_node, to_node, written_by) "
            "VALUES (?, 'association', 'contradicts', ?, ?, 'system')",
            (edge_id, deriv_id, l0_id),
        )
        # Find descendants of target (l0_id)
        desc_rows = con.execute(
            """
            WITH RECURSIVE descendants AS (
                SELECT e.from_node AS id
                FROM edge e
                WHERE e.to_node = ?
                  AND e.type = 'provenance'
                  AND e.relation = 'derived_from'
                UNION ALL
                SELECT e.from_node
                FROM edge e
                JOIN descendants d ON e.to_node = d.id
                WHERE e.type = 'provenance'
                  AND e.relation = 'derived_from'
            )
            SELECT id FROM descendants
            """,
            (l0_id,),
        ).fetchall()
        descendants = [r[0] for r in desc_rows]
        all_nodes = [l0_id] + descendants
        # Event queue
        cur = con.execute(
            "INSERT INTO event_queue (event_type, edge_id, target_node_id, created_at, status) "
            "VALUES ('contradicts_edge_needs_review', ?, ?, ?, 'pending')",
            (edge_id, l0_id, now),
        )
        event_id = cur.lastrowid
        # Event-node links
        for node_id in all_nodes:
            con.execute(
                "INSERT INTO event_node_link (event_id, node_id, contested_at) VALUES (?, ?, ?)",
                (event_id, node_id, now),
            )
            con.execute(
                "UPDATE node SET is_contested = 1, contested_at = ? WHERE id = ? AND is_contested = 0",
                (now, node_id),
            )
        con.commit()
        con.close()
        return edge_id

    # ── Scenario 1: Full accept flow ───────────────────────────
    print("  [SCENARIO 1] Full accept flow")
    db1, vault1 = _fresh_store(tmp, "review1")

    # Register L0
    md_path = _register_file(vault1, "review1.md", "https://example.com/article")
    proc = _run(
        ["register", "--db", str(db1), "--vault", str(vault1), str(md_path)],
    )
    l0 = _expect_json("sc1 register URL", proc)
    l0_id = l0["id"]

    # Derive child
    proc = _run(
        ["derive", "--db", str(db1), "--vault", str(vault1), l0_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )
    deriv = _expect_json("sc1 derive child", proc)
    deriv_id = deriv["id"]

    # Create contradicts edge via CLI command (end-to-end test)
    proc = _run(
        ["contradict", "--db", str(db1), "--vault", str(vault1), l0_id, "--asserted-by", deriv_id],
    )
    contra = _expect_json("sc1 contradict", proc)
    _check("sc1 contradict returns edge_id", "edge_id" in contra, f"got {contra}")
    _check("sc1 contradict target_node_id matches", contra.get("target_node_id") == l0_id)
    _check("sc1 contradict written_by=human", contra.get("written_by") == "human")
    contra_edge_id = contra["edge_id"]

    # Verify event was created (propagation happened)
    con = sqlite3.connect(str(db1))
    link_rows = con.execute(
        "SELECT node_id FROM event_node_link enl "
        "JOIN event_queue eq ON eq.id = enl.event_id "
        "WHERE eq.edge_id = ?", (contra_edge_id,)
    ).fetchall()
    _check("sc1 event_node_link has 2 rows (L0 + deriv)", len(link_rows) == 2, f"got {len(link_rows)}")
    con.close()

    # Create temp fake LLM client
    fake_path1 = tmp / "ReviewFake1.py"
    mod_str1 = _make_fake_review(fake_path1, [l0_id, deriv_id])
    review_env1 = {
        "MEMEX_AGENT": mod_str1,
        "PYTHONPATH": str(tmp),
    }

    # Run memex review (batch)
    proc = _run(
        ["review", "--db", str(db1), "--vault", str(vault1)],
        env=review_env1,
    )
    review_res = _expect_json("sc1 review batch", proc)
    _check("sc1 review processed=1", review_res.get("processed") == 1, f"got {review_res}")
    proposals = review_res.get("proposals", [])
    _check("sc1 review has 1 proposal", len(proposals) == 1, f"got {len(proposals)}")
    _check("sc1 proposal status=proposed", proposals[0].get("status") == "proposed", f"got {proposals[0]}")
    sc1_proposal_id = proposals[0]["proposal_id"]

    # review list
    proc = _run(
        ["review", "--db", str(db1), "--vault", str(vault1), "list"],
    )
    list_res = _expect_json("sc1 review list", proc)
    _check("sc1 list contains pending proposal", any(
        item.get("kind") == "pending_proposal" and item.get("id") == sc1_proposal_id
        for item in (list_res if isinstance(list_res, list) else [])
    ), f"got {list_res}")
    # review accept
    proc = _run(
        ["review", "--db", str(db1), "--vault", str(vault1), "accept", str(sc1_proposal_id)],
    )
    accept_res = _expect_json("sc1 review accept", proc)
    _check("sc1 accept status=accepted", accept_res.get("status") == "accepted", f"got {accept_res}")

    # Verify via SQL
    con = sqlite3.connect(str(db1))
    con.row_factory = sqlite3.Row
    l0_row = con.execute(
        "SELECT trust_state, is_contested FROM node WHERE id = ?", (l0_id,)
    ).fetchone()
    deriv_row = con.execute(
        "SELECT trust_state, is_contested FROM node WHERE id = ?", (deriv_id,)
    ).fetchone()
    con.close()
    _check("sc1 L0 trust_state=stale", l0_row["trust_state"] == "stale", f"got {dict(l0_row)}")
    _check("sc1 deriv trust_state=stale", deriv_row["trust_state"] == "stale", f"got {dict(deriv_row)}")
    _check("sc1 L0 is_contested=0", l0_row["is_contested"] == 0, f"got {dict(l0_row)}")
    _check("sc1 deriv is_contested=0", deriv_row["is_contested"] == 0, f"got {dict(deriv_row)}")

    # ── Scenario 2: Multi-event coverage ───────────────────────
    print("  [SCENARIO 2] Multi-event coverage")
    db2, vault2 = _fresh_store(tmp, "review2")

    # Register L0
    md_path = _register_file(vault2, "review2.md", "https://example.com/article")
    proc = _run(
        ["register", "--db", str(db2), "--vault", str(vault2), str(md_path)],
    )
    l0_2 = _expect_json("sc2 register URL", proc)
    l0_2_id = l0_2["id"]

    # Derive child
    proc = _run(
        ["derive", "--db", str(db2), "--vault", str(vault2), l0_2_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )
    deriv_2 = _expect_json("sc2 derive child", proc)
    deriv_2_id = deriv_2["id"]

    # Create two contradicts edges
    _create_contradiction(db2, deriv_2_id, l0_2_id)
    _create_contradiction(db2, deriv_2_id, l0_2_id)

    # Create fake LLM for scenario 2
    fake_path2 = tmp / "ReviewFake2.py"
    mod_str2 = _make_fake_review(fake_path2, [l0_2_id, deriv_2_id])
    review_env2 = {
        "MEMEX_AGENT": mod_str2,
        "PYTHONPATH": str(tmp),
    }

    # Run review batch (should generate 2 proposals)
    proc = _run(
        ["review", "--db", str(db2), "--vault", str(vault2)],
        env=review_env2,
    )
    review_res2 = _expect_json("sc2 review batch", proc)
    _check("sc2 review processed=2", review_res2.get("processed") == 2, f"got {review_res2}")

    # Accept first proposal
    props2 = review_res2["proposals"]
    first_pid = props2[0]["proposal_id"]
    proc = _run(
        ["review", "--db", str(db2), "--vault", str(vault2), "accept", str(first_pid)],
    )
    accept_2a = _expect_json("sc2 accept first", proc)
    _check("sc2 accept first ok", accept_2a.get("status") == "accepted", f"got {accept_2a}")

    # Verify L0 still contested (second event still open)
    con = sqlite3.connect(str(db2))
    l0_2_row = con.execute(
        "SELECT is_contested FROM node WHERE id = ?", (l0_2_id,)
    ).fetchone()
    con.close()
    _check("sc2 L0 still contested after 1st accept", l0_2_row[0] == 1, f"is_contested={l0_2_row[0]}")

    # Accept second proposal
    second_pid = props2[1]["proposal_id"]
    proc = _run(
        ["review", "--db", str(db2), "--vault", str(vault2), "accept", str(second_pid)],
    )
    accept_2b = _expect_json("sc2 accept second", proc)
    _check("sc2 accept second ok", accept_2b.get("status") == "accepted", f"got {accept_2b}")

    # Verify L0 no longer contested
    con = sqlite3.connect(str(db2))
    l0_2_row = con.execute(
        "SELECT is_contested FROM node WHERE id = ?", (l0_2_id,)
    ).fetchone()
    con.close()
    _check("sc2 L0 not contested after 2nd accept", l0_2_row[0] == 0, f"is_contested={l0_2_row[0]}")

    # ── Scenario 3: Reject preserves trust_state ───────────────
    print("  [SCENARIO 3] Reject preserves trust_state")
    db3, vault3 = _fresh_store(tmp, "review3")

    # Register L0
    md_path = _register_file(vault3, "review3.md", "https://example.com/article")
    proc = _run(
        ["register", "--db", str(db3), "--vault", str(vault3), str(md_path)],
    )
    l0_3 = _expect_json("sc3 register URL", proc)
    l0_3_id = l0_3["id"]

    # Derive child
    proc = _run(
        ["derive", "--db", str(db3), "--vault", str(vault3), l0_3_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )
    deriv_3 = _expect_json("sc3 derive child", proc)
    deriv_3_id = deriv_3["id"]

    # Mark L0 as human-approved
    con = sqlite3.connect(str(db3))
    con.execute("UPDATE node SET trust_state = 'human-approved' WHERE id = ?", (l0_3_id,))
    con.commit()
    con.close()

    # --- Part A: Accept overrides human-approved ---
    _create_contradiction(db3, deriv_3_id, l0_3_id)

    fake_path3a = tmp / "ReviewFake3a.py"
    mod_str3a = _make_fake_review(fake_path3a, [l0_3_id, deriv_3_id])
    review_env3a = {
        "MEMEX_AGENT": mod_str3a,
        "PYTHONPATH": str(tmp),
    }

    proc = _run(
        ["review", "--db", str(db3), "--vault", str(vault3)],
        env=review_env3a,
    )
    review_res3a = _expect_json("sc3a review batch", proc)
    proposal_3a_id = review_res3a["proposals"][0]["proposal_id"]

    proc = _run(
        ["review", "--db", str(db3), "--vault", str(vault3), "accept", str(proposal_3a_id)],
    )
    accept_3a = _expect_json("sc3a review accept", proc)
    _check("sc3a accept status=accepted", accept_3a.get("status") == "accepted", f"got {accept_3a}")

    con = sqlite3.connect(str(db3))
    l0_3_row = con.execute(
        "SELECT trust_state FROM node WHERE id = ?", (l0_3_id,)
    ).fetchone()
    con.close()
    _check("sc3a L0 trust_state=stale after accept (override human-approved)",
           l0_3_row[0] == "stale", f"got {l0_3_row[0]}")

    # --- Part B: Reject preserves trust_state ---
    _create_contradiction(db3, deriv_3_id, l0_3_id)

    fake_path3b = tmp / "ReviewFake3b.py"
    mod_str3b = _make_fake_review(fake_path3b, [l0_3_id, deriv_3_id])
    review_env3b = {
        "MEMEX_AGENT": mod_str3b,
        "PYTHONPATH": str(tmp),
    }

    proc = _run(
        ["review", "--db", str(db3), "--vault", str(vault3)],
        env=review_env3b,
    )
    review_res3b = _expect_json("sc3b review batch", proc)
    _check("sc3b review processed=1", review_res3b.get("processed") == 1, f"got {review_res3b}")
    proposal_3b_id = review_res3b["proposals"][0]["proposal_id"]

    proc = _run(
        ["review", "--db", str(db3), "--vault", str(vault3), "reject", str(proposal_3b_id)],
    )
    reject_3b = _expect_json("sc3b review reject", proc)
    _check("sc3b reject status=rejected", reject_3b.get("status") == "rejected", f"got {reject_3b}")

    con = sqlite3.connect(str(db3))
    l0_3_row_b = con.execute(
        "SELECT trust_state FROM node WHERE id = ?", (l0_3_id,)
    ).fetchone()
    con.close()
    _check("sc3b L0 trust_state unchanged after reject",
           l0_3_row_b[0] == "stale", f"got {l0_3_row_b[0]}")

    # ── Scenario 4: Dismiss preserves trust_state ──────────────
    print("  [SCENARIO 4] Dismiss preserves trust_state")
    db4, vault4 = _fresh_store(tmp, "review4")

    # Register L0
    md_path = _register_file(vault4, "review4.md", "https://example.com/article")
    proc = _run(
        ["register", "--db", str(db4), "--vault", str(vault4), str(md_path)],
    )
    l0_4 = _expect_json("sc4 register URL", proc)
    l0_4_id = l0_4["id"]

    # Derive child
    proc = _run(
        ["derive", "--db", str(db4), "--vault", str(vault4), l0_4_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )
    deriv_4 = _expect_json("sc4 derive child", proc)
    deriv_4_id = deriv_4["id"]

    # Create contradicts edge
    _create_contradiction(db4, deriv_4_id, l0_4_id)

    # Create temp fake LLM
    fake_path4 = tmp / "ReviewFake4.py"
    mod_str4 = _make_fake_review(fake_path4, [l0_4_id, deriv_4_id])
    review_env4 = {
        "MEMEX_AGENT": mod_str4,
        "PYTHONPATH": str(tmp),
    }

    # Run review batch
    proc = _run(
        ["review", "--db", str(db4), "--vault", str(vault4)],
        env=review_env4,
    )
    review_res4 = _expect_json("sc4 review batch", proc)
    _check("sc4 review processed=1", review_res4.get("processed") == 1, f"got {review_res4}")
    proposal_4_id = review_res4["proposals"][0]["proposal_id"]

    # Dismiss
    proc = _run(
        ["review", "--db", str(db4), "--vault", str(vault4), "dismiss", str(proposal_4_id)],
    )
    dismiss_4 = _expect_json("sc4 review dismiss", proc)
    _check("sc4 dismiss status=dismissed", dismiss_4.get("status") == "dismissed", f"got {dismiss_4}")

    # Verify trust_state unchanged (L0 is still draft)
    con = sqlite3.connect(str(db4))
    l0_4_row = con.execute(
        "SELECT trust_state, is_contested FROM node WHERE id = ?", (l0_4_id,)
    ).fetchone()
    con.close()
    _check("sc4 L0 trust_state=draft after dismiss", l0_4_row[0] == "draft", f"got {l0_4_row[0]}")
    _check("sc4 L0 is_contested=0 after dismiss", l0_4_row[1] == 0, f"got {l0_4_row[1]}")


def smoke_auto_defaults(tmp: Path) -> None:
    """Auto-detection and fallback of --vault and --db defaults."""
    print("\n[AUTO-DEFAULTS] vault/db auto-detection edge cases")

    # Scenario 1: explicit --db and --vault (unchanged behavior)
    vp = tmp / "vault"
    dp = tmp / "vault/.memex/memex.db"
    proc = _run(["init", "--db", str(dp), "--vault", str(vp)])
    r = _expect_json("init explicit", proc)
    _check("explicit db matches", r.get("db_path") == str(dp))
    _check("explicit vault matches", r.get("vault_path") == str(vp))
    _check("explicit db_created", r.get("db_created") is True)

    # Scenario 2: --vault only (db derived from vault)
    proc = _run(["status", "--vault", str(vp)])
    r = _expect_json("status --vault only", proc)
    _check("status --vault db derived", r.get("db_path") == str(dp))
    _check("status --vault db_exists", r.get("db_exists") is True)
    _check("status --vault vault matches", r.get("vault_path") == str(vp))

    # Scenario 3: --db only (vault falls back to ~/memex-vault when no Obsidian detected)
    dp2 = tmp / "custom.db"
    proc = _run(["status", "--db", str(dp2)])
    r = _expect_json("status --db only", proc)
    _check("status --db matches", r.get("db_path") == str(dp2))
    _check("status --db vault_path is set", bool(r.get("vault_path")))
    _check("status --db vault_exists field", r.get("vault_exists") is not None)



def smoke_resolve(tmp: Path) -> None:
    """End-to-end tests for memex resolve command."""
    print("  [RESOLVE] arXiv abs -> PDF")
    p = _run(["resolve", "https://arxiv.org/abs/2304.12345"])
    _check("resolve arxiv exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve arxiv type", d["type"] == "arxiv")
    _check("resolve arxiv direct_url", d["direct_url"] == "https://arxiv.org/pdf/2304.12345")
    _check("resolve arxiv ingestable", d["ingestable"] is True)

    print("  [RESOLVE] GitHub blob -> raw")
    p = _run(["resolve", "https://github.com/user/repo/blob/main/file.py"])
    _check("resolve github exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve github type", d["type"] == "github_file")
    _check("resolve github direct_url", d["direct_url"] == "https://raw.githubusercontent.com/user/repo/main/file.py")

    print("  [RESOLVE] Wikipedia -> REST API")
    p = _run(["resolve", "https://en.wikipedia.org/wiki/Python_(programming_language)"])
    _check("resolve wikipedia exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve wikipedia type", d["type"] == "wikipedia")
    _check("resolve wikipedia direct_url", d["direct_url"].startswith("https://en.wikipedia.org/api/rest_v1/page/summary/"))

    print("  [RESOLVE] Web article -> type web")
    p = _run(["resolve", "https://example.com/article"])
    _check("resolve web exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve web ingestable", d["ingestable"] is True)

    print("  [RESOLVE] Media URL (jpg) -> not ingestable")
    p = _run(["resolve", "https://example.com/photo.jpg"])
    _check("resolve jpg exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve jpg type", d["type"] == "unknown")
    _check("resolve jpg ingestable", d["ingestable"] is False)

    print("  [RESOLVE] X/Twitter -> not ingestable with note")
    p = _run(["resolve", "https://x.com/user/status/123"])
    _check("resolve x exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve x ingestable", d["ingestable"] is False)
    _check("resolve x has note", "note" in d)

    print("  [RESOLVE] Missing URL -> JSON error")
    p = _run(["resolve"])
    _check("resolve missing exits non-zero", p.returncode != 0)
    d = json.loads(p.stderr)
    _check("resolve missing has error", "error" in d)

    print("  [RESOLVE] Empty URL -> JSON error")
    p = _run(["resolve", ""])
    _check("resolve empty exits non-zero", p.returncode != 0)
    d = json.loads(p.stderr)
    _check("resolve empty has error", "error" in d)

    print("  [RESOLVE] URL with tracking params -> still resolves")
    p = _run(["resolve", "https://arxiv.org/abs/2304.12345?utm_source=twitter&fbclid=abc"])
    _check("resolve tracking exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve tracking type", d["type"] == "arxiv")
    _check("resolve tracking direct_url", d["direct_url"] == "https://arxiv.org/pdf/2304.12345")

    print("  [RESOLVE] GitHub with query params -> still resolves")
    p = _run(["resolve", "https://github.com/user/repo/blob/main/file.py?token=abc"])
    _check("resolve github query exits 0", p.returncode == 0)
    d = json.loads(p.stdout)
    _check("resolve github query type", d["type"] == "github_file")
    _check("resolve github query direct_url", d["direct_url"] == "https://raw.githubusercontent.com/user/repo/main/file.py")


# ── runner ──────────────────────────────────────────────────────


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        smoke_lifecycle(tmp)
        smoke_idempotency(tmp)
        smoke_derive_passing(tmp)
        smoke_derive_failing(tmp)
        smoke_derive_idempotent(tmp)
        smoke_derive_all(tmp)
        smoke_search(tmp)
        smoke_errors(tmp)
        smoke_migration(tmp)
        smoke_render(tmp)
        smoke_review(tmp)
        smoke_auto_defaults(tmp)
        smoke_help(tmp)
        smoke_full_e2e(tmp)
        smoke_resolve(tmp)

    print(f"\n{'='*60}")
    print(f"PASSED: {_passes}    FAILED: {len(_failures)}")
    if _failures:
        print("\nFailures:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All aggressive smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
