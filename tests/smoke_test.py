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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKE_FETCHER = "tests.conftest:FakeFetcher"
FAKE_LLM = "tests.fake_llm_client:FakeLLMClient"
FAKE_TELEGRAM = "tests.fake_telegram_source:FakeTelegramSource"
FAKE_FAILING_LLM = "tests.fake_llm_client_failing:FakeLLMClientFailing"


# ── harness ──────────────────────────────────────────────────────


class SmokeError(AssertionError):
    pass


_failures: list[str] = []
_passes = 0


def _run(args: list[str], env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    # `uv run python -m memex.cli` keeps cwd on sys.path so the
    # `MEMEX_FETCHER_MODULE=tests.conftest:FakeFetcher` test seam works
    # without PYTHONPATH. Falls back to direct python if uv is absent.
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


# ── smoke groups ─────────────────────────────────────────────────


def smoke_lifecycle(tmp: Path) -> None:
    print("\n[LIFECYCLE] init → status → ingest → list → show")
    db, vault = _fresh_store(tmp, "lifecycle")
    s = _run(["status", "--db", str(db), "--vault", str(vault)])
    d = _expect_json("status after init", s)
    _check("status reports exists=true after init", d["db_exists"] and d["vault_exists"])

    proc = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/article"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    d = _expect_json("ingest URL", proc)
    _check("ingest returns id", "id" in d)
    _check("ingest status=ingested", d.get("status") == "ingested")
    node_id = d["id"]

    # L0 file actually exists on disk
    md_files = list(vault.glob("*.md"))
    _check("vault has 1 md file", len(md_files) == 1, f"got {len(md_files)}")

    # L0 file content matches what the fetcher produced
    content = md_files[0].read_text()
    _check("L0 file contains fetched content", "Fake content" in content)

    proc = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list", proc)
    _check("list returns 1 node", len(lst) == 1, f"got {len(lst)}")
    _check("list node has expected fields",
           set(lst[0].keys()) >= {"id", "kind", "tier", "trust_state", "canonical_key"})
    _check("list node kind=raw_source", lst[0]["kind"] == "raw_source")
    _check("list node trust_state=draft", lst[0]["trust_state"] == "draft")
    _check("list node canonical_key stripped of utm", lst[0]["canonical_key"] == "https://example.com/article")

    proc = _run(["show", "--db", str(db), "--vault", str(vault), node_id])
    sh = _expect_json("show", proc)
    _check("show returns the node", sh["id"] == node_id)
    _check("show includes content", sh.get("content") is not None)
    _check("show check_failures is null for L0", sh.get("check_failures") is None)
    _check("show l0_path set", sh.get("l0_path") is not None)


def smoke_idempotency(tmp: Path) -> None:
    print("\n[IDEMPOTENCY] ingest same URL twice → one node")
    db, vault = _fresh_store(tmp, "idem")
    url = "https://example.com/article?utm_source=twitter"
    env = {"MEMEX_FETCHER_MODULE": FAKE_FETCHER}
    p1 = _run(["ingest", "--db", str(db), "--vault", str(vault), url], env=env)
    d1 = _expect_json("ingest #1", p1)
    p2 = _run(["ingest", "--db", str(db), "--vault", str(vault), url], env=env)
    d2 = _expect_json("ingest #2", p2)
    _check("same id returned", d1["id"] == d2["id"])
    _check("second status=already_exists", d2.get("status") == "already_exists")

    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    s = con.execute("SELECT COUNT(*) FROM source").fetchone()[0]
    con.close()
    _check("exactly 1 node row", n == 1, f"got {n}")
    _check("exactly 1 source row", s == 1, f"got {s}")


def smoke_fetch_failure(tmp: Path) -> None:
    print("\n[FETCH FAILURE] ingest URL that fails to fetch")
    db, vault = _fresh_store(tmp, "fetchfail")
    proc = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "https://fail.example.com/bad"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    # exit 0 (failure recorded, not crashed)
    d = _expect_json("ingest fail", proc)
    _check("status=fetch_failed", d.get("status") == "fetch_failed")
    _check("error message present", "error" in d)

    con = sqlite3.connect(db)
    failed = con.execute("SELECT failed FROM source").fetchone()[0]
    nodes = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    con.close()
    _check("source.failed=1 in db", failed == 1, f"got {failed}")
    _check("node row still created", nodes == 1, f"got {nodes}")
    # L0 file should NOT be written for failed fetches
    md_files = list(vault.glob("*.md"))
    _check("no L0 markdown for failed fetch", len(md_files) == 0, f"got {len(md_files)}")


def smoke_inbox(tmp: Path) -> None:
    print("\n[INBOX] ingest --inbox from a WhatsApp export")
    db, vault = _fresh_store(tmp, "inbox")

    export = """\
[01/06/2024, 09:15:32] Alice: https://example.com/article
[01/06/2024, 10:00:00] Bob: Check this out https://news.example.com/story interesting read
[01/06/2024, 11:30:45] Alice: Just catching up, no links here
[02/06/2024, 08:00:00] Bob: https://blog.example.com/post?utm_source=twitter
[02/06/2024, 09:00:00] Alice: Morning!
"""
    inbox_path = tmp / "inbox.txt"
    inbox_path.write_text(export, encoding="utf-8")

    proc = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "--inbox", str(inbox_path)],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    results = _expect_json("ingest --inbox", proc)
    _check("ingested 3 URLs (one msg had no link)", len(results) == 3, f"got {len(results)}")
    _check("all statuses=ingested", all(r["status"] == "ingested" for r in results))

    con = sqlite3.connect(db)
    inbox_count = con.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    cursor_row = con.execute("SELECT value FROM cursor").fetchone()
    con.close()
    _check("3 inbox rows", inbox_count == 3, f"got {inbox_count}")
    _check("3 nodes created", node_count == 3, f"got {node_count}")
    _check("cursor advanced to 3", cursor_row[0] == "3", f"got {cursor_row}")

    # Re-run: cursor moved past, so nothing is processed
    proc = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "--inbox", str(inbox_path)],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    rerun = _expect_json("re-run ingest --inbox", proc)
    _check("re-run is no-op (empty results)", rerun == [])

    con = sqlite3.connect(db)
    inbox_count = con.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    con.close()
    _check("no duplicate inbox rows on re-run", inbox_count == 3, f"got {inbox_count}")
    _check("no duplicate nodes on re-run", node_count == 3, f"got {node_count}")


def smoke_inbox_with_failures(tmp: Path) -> None:
    print("\n[INBOX FAILURES] a failing URL in inbox still gets a ledger row")
    db, vault = _fresh_store(tmp, "inboxfail")
    export = (
        "[01/06/2024, 09:15:32] Alice: https://example.com/ok\n"
        "[01/06/2024, 09:16:00] Bob: https://fail.example.com/oops\n"
    )
    inbox_path = tmp / "inbox.txt"
    inbox_path.write_text(export, encoding="utf-8")
    proc = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "--inbox", str(inbox_path)],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    results = _expect_json("ingest --inbox mixed", proc)
    _check("got 2 results", len(results) == 2, f"got {len(results)}")
    statuses = sorted(r["status"] for r in results)
    _check("one ingested, one fetch_failed", statuses == ["fetch_failed", "ingested"],
           f"got {statuses}")

    # Both must appear in the inbox table (capture happens before fetch)
    con = sqlite3.connect(db)
    inbox_urls = {r[0] for r in con.execute("SELECT url FROM inbox").fetchall()}
    con.close()
    _check("both URLs recorded in inbox", len(inbox_urls) == 2, f"got {inbox_urls}")


def smoke_pending(tmp: Path) -> None:
    print("\n[PENDING] list --pending surfaces captured-but-not-ingested keys")
    db, vault = _fresh_store(tmp, "pending")
    # Capture-only (no ingest): insert directly into inbox
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("whatsapp:test", "https://example.com/a", "2024-06-01T09:00:00", None, "2024-06-01T09:00:00"),
            ("whatsapp:test", "https://example.com/b", "2024-06-01T09:00:01", None, "2024-06-01T09:00:01"),
        ],
    )
    con.commit()
    con.close()

    proc = _run(["list", "--db", str(db), "--vault", str(vault), "--pending"])
    pending = _expect_json("list --pending", proc)
    _check("returns 2 pending keys", len(pending) == 2, f"got {pending}")
    _check("keys are canonical (no utm)",
           "https://example.com/a" in pending and "https://example.com/b" in pending)

    # After ingesting one, only one remains pending
    _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/a"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    proc = _run(["list", "--db", str(db), "--vault", str(vault), "--pending"])
    pending = _expect_json("list --pending after one ingest", proc)
    _check("1 pending remaining", len(pending) == 1 and pending[0] == "https://example.com/b",
           f"got {pending}")


def smoke_derive_passing(tmp: Path) -> None:
    print("\n[DERIVE PASS] derive → auto-verified")
    db, vault = _fresh_store(tmp, "deriveok")
    p = _run(["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/article"],
             env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER})
    l0_id = _expect_json("ingest for derive", p)["id"]

    p = _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_LLM_MODULE": FAKE_LLM})
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
    md_files = list(vault.glob("*.md"))
    _check("2 md files (l0 + deriv)", len(md_files) == 2, f"got {len(md_files)}")
    deriv_md = vault / f"{deriv_id}.md"
    _check("derivation md exists", deriv_md.exists())
    _check("derivation has > Synthesis: marker", "> Synthesis:" in deriv_md.read_text())


def smoke_derive_failing(tmp: Path) -> None:
    print("\n[DERIVE FAIL] failing LLM → draft with check_failures")
    db, vault = _fresh_store(tmp, "derivefail")
    p = _run(["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/article"],
             env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER})
    l0_id = _expect_json("ingest for failing derive", p)["id"]

    p = _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_LLM_MODULE": FAKE_FAILING_LLM})
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
    print("\n[DERIVE IDEMPOTENT] derive twice → already_derived")
    db, vault = _fresh_store(tmp, "deriveidem")
    p = _run(["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/article"],
             env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER})
    l0_id = _expect_json("ingest", p)["id"]

    _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
         env={"MEMEX_LLM_MODULE": FAKE_LLM})
    p = _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_LLM_MODULE": FAKE_LLM})
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
    env = {"MEMEX_FETCHER_MODULE": FAKE_FETCHER, "MEMEX_LLM_MODULE": FAKE_LLM}

    # Ingest 3 URLs
    for i in range(3):
        p = _run(["ingest", "--db", str(db), "--vault", str(vault),
                  f"https://example.com/article-{i}"], env=env)
        _expect_json(f"ingest {i}", p)

    # Derive first one manually
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
    _check("2 results (2 already + 1 new)", len(res) == 3, f"got {len(res)}")
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
    p = _run(["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/article"],
             env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER})
    l0_id = _expect_json("ingest for search", p)["id"]
    _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
         env={"MEMEX_LLM_MODULE": FAKE_LLM})

    p = _run(["search", "--db", str(db), "--vault", str(vault), "broader pattern"])
    res = _expect_json("search", p)
    _check("search returns ≥1 result", len(res) >= 1, f"got {res}")
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
             env={"MEMEX_LLM_MODULE": FAKE_LLM})
    _check("derive unknown id exits non-zero", p.returncode != 0)

    # ingest with no URL and no --inbox
    p = _run(["ingest", "--db", str(db), "--vault", str(vault)])
    _check("ingest with no url/inbox exits non-zero", p.returncode != 0)

    # ingest with --inbox pointing at nonexistent file
    p = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "--inbox", str(tmp / "nope.txt")],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    _check("ingest with missing inbox file exits non-zero", p.returncode != 0)


def smoke_migration(tmp: Path) -> None:
    print("\n[MIGRATION] old-schema DB (no inbox, no check_failures) → init → use")
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
        CREATE TABLE IF NOT EXISTS cursor (
            source_name TEXT PRIMARY KEY,
            value       TEXT NOT NULL
        );
    """)
    con.commit()
    con.close()
    vault.mkdir(parents=True, exist_ok=True)

    # init must not crash, must add inbox + check_failures + failed columns
    p = _run(["init", "--db", str(db), "--vault", str(vault)])
    _expect_json("init on old schema", p)

    con = sqlite3.connect(db)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall() if not r[0].startswith("sqlite_")}
    node_cols = {r[1] for r in con.execute("PRAGMA table_info(node)").fetchall()}
    source_cols = {r[1] for r in con.execute("PRAGMA table_info(source)").fetchall()}
    con.close()
    _check("inbox table created", "inbox" in tables, f"got {tables}")
    _check("check_failures column added", "check_failures" in node_cols, f"got {node_cols}")
    _check("failed column added to source", "failed" in source_cols, f"got {source_cols}")

    # And ingest still works on the migrated DB
    p = _run(["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/article"],
             env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER})
    d = _expect_json("ingest after migration", p)
    _check("ingest works after migration", d.get("status") == "ingested")


def smoke_youtube(tmp: Path) -> None:
    print("\n[YOUTUBE] canonical_key maps youtube URLs to stable scheme")
    db, vault = _fresh_store(tmp, "yt")
    p = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    d = _expect_json("ingest youtube", p)
    _check("youtube canonical_key uses scheme", d.get("canonical_key") == "youtube://dQw4w9WgXcQ",
           f"got {d.get('canonical_key')}")

    # Same video via youtu.be → same key
    p = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "https://youtu.be/dQw4w9WgXcQ"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    d = _expect_json("ingest youtu.be", p)
    _check("youtu.be dedupes to same node", d.get("status") == "already_exists")


def smoke_l0_immutable(tmp: Path) -> None:
    print("\n[L0 IMMUTABLE] L0 markdown file is not overwritten on re-ingest")
    db, vault = _fresh_store(tmp, "immutable")
    url = "https://example.com/article"
    env = {"MEMEX_FETCHER_MODULE": FAKE_FETCHER}
    _run(["ingest", "--db", str(db), "--vault", str(vault), url], env=env)
    md_files = list(vault.glob("*.md"))
    _check("1 md file after first ingest", len(md_files) == 1)

    # Mutate the file externally to prove re-ingest doesn't overwrite it.
    md_files[0].write_text("MUTATED CONTENT")
    original_mtime = md_files[0].stat().st_mtime

    _run(["ingest", "--db", str(db), "--vault", str(vault), url], env=env)
    after = md_files[0].read_text()
    _check("L0 file content preserved on re-ingest", after == "MUTATED CONTENT",
           f"got {after!r}")
    _check("L0 file mtime preserved (no rewrite)", md_files[0].stat().st_mtime == original_mtime,
           f"mtime changed from {original_mtime} to {md_files[0].stat().st_mtime}")
    # (sanity: the node count is still 1, so re-ingest didn't duplicate)
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    con.close()
    _check("node still unique", n == 1, f"got {n}")


def smoke_render(tmp: Path) -> None:
    print("\n[RENDER] core render — metadata + tags + aliases")
    db, vault = _fresh_store(tmp, "render")

    # L0 render
    _run(["ingest", "--db", str(db), "--vault", str(vault), "https://example.com/article"],
         env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER})

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
             env={"MEMEX_LLM_MODULE": FAKE_LLM})
    d = _expect_json("derive for render", p)
    deriv_id = d["id"]

    p = _run(["render", "--db", str(db), "--vault", str(vault)])
    res = _expect_json("render with derivations", p)
    _check("2 nodes rendered", len(res) == 2, f"got {len(res)}")

    deriv_md = vault / f"{deriv_id}.md"
    dtext = deriv_md.read_text(encoding="utf-8")
    _, dfm_raw, _ = dtext.split("---\n", 2)
    dfm = _y.safe_load(dfm_raw)
    _check("derivation frontmatter has kind=summary", dfm.get("kind") == "summary")
    _check("derivation frontmatter has tier=notes", dfm.get("tier") == "notes")
    _check("derivation frontmatter has trust_state", dfm.get("trust_state") in ("draft", "auto-verified"))
    _check("derivation frontmatter has check_failures", isinstance(dfm.get("check_failures"), list))
    _check("derivation tags include kind/summary", "kind/summary" in dfm.get("tags", []))
    _check("derivation tags include tier/notes", "tier/notes" in dfm.get("tags", []))

    # Derivation render includes derived_from wikilink
    _check("derived_from wikilink present", "derived_from" in dfm, f"keys: {list(dfm.keys())}")
    _check("derived_from is scalar [[uuid]]",
           isinstance(dfm["derived_from"], str),
           f"got {type(dfm['derived_from']).__name__}: {dfm['derived_from']}")
    _check("derived_from begins with [[", dfm["derived_from"].startswith("[["),
           f"got {dfm['derived_from']!r}")

    # Empty vault returns empty
    db2, vault2 = _fresh_store(tmp, "render-empty")
    p = _run(["render", "--db", str(db2), "--vault", str(vault2)])
    res_empty = _expect_json("render empty", p)
    _check("empty vault returns []", res_empty == [])

    # Missing DB exits error
    p = _run(["render", "--db", str(tmp / "nope.db"), "--vault", str(vault2)])
    _check("render missing DB exits non-zero", p.returncode != 0)


def smoke_capture(tmp: Path) -> None:
    print("\n[CAPTURE] memex capture + ingest --from-inbox loop")
    db, vault = _fresh_store(tmp, "capture")

    p = _run(
        ["capture", "--db", str(db), "--vault", str(vault)],
        env={"MEMEX_TELEGRAM_SOURCE": FAKE_TELEGRAM},
    )
    res = _expect_json("capture", p)
    _check("capture returns items", len(res) >= 1, f"got {len(res)}")

    # list --pending shows them
    p = _run(["list", "--db", str(db), "--vault", str(vault), "--pending"])
    pending = _expect_json("list --pending after capture", p)
    _check("pending non-empty after capture", len(pending) >= 1, f"got {len(pending)}")

    # ingest --from-inbox flushes them
    p = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "--from-inbox"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    res = _expect_json("ingest --from-inbox after capture", p)
    _check("from-inbox ingested items", len(res) >= 1, f"got {len(res)}")
    _check("all ingested or already_exists",
           all(r["status"] in ("ingested", "already_exists") for r in res))

    # pending cleared
    p = _run(["list", "--db", str(db), "--vault", str(vault), "--pending"])
    pending = _expect_json("list --pending cleared", p)
    _check("pending empty after ingest", pending == [], f"got {pending}")

    # Re-run capture is no-op (cursor advanced)
    p = _run(
        ["capture", "--db", str(db), "--vault", str(vault)],
        env={"MEMEX_TELEGRAM_SOURCE": FAKE_TELEGRAM},
    )
    res = _expect_json("capture #2", p)
    _check("capture re-run returns empty", res == [], f"got {len(res)} items")

    # No source configured
    p = _run(["capture", "--db", str(db), "--vault", str(vault)])
    _check("capture without source exits non-zero", p.returncode != 0)


def smoke_from_inbox(tmp: Path) -> None:
    print("\n[FROM-INBOX] memex ingest --from-inbox")
    db, vault = _fresh_store(tmp, "frominbox")

    # Pre-fill inbox with a few URLs
    con = sqlite3.connect(db)
    now = "2024-06-01T09:00:00"
    for url in ["https://example.com/alpha", "https://example.com/beta"]:
        con.execute(
            "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("smoke:test", url, now, None, now),
        )
    con.commit()
    con.close()

    p = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "--from-inbox"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    res = _expect_json("ingest --from-inbox", p)
    _check("returns 2 results", len(res) == 2, f"got {len(res)}")
    _check("both ingested", all(r["status"] in ("ingested", "already_exists") for r in res))

    # Re-run is idempotent
    p = _run(
        ["ingest", "--db", str(db), "--vault", str(vault), "--from-inbox"],
        env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER},
    )
    res = _expect_json("ingest --from-inbox #2", p)
    _check("re-run still returns 2", len(res) == 2, f"got {len(res)}")
    _check("re-run shows already_exists", all(r["status"] == "already_exists" for r in res))

    # Inbox rows preserved
    con = sqlite3.connect(db)
    inbox_count = con.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
    node_count = con.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    con.close()
    _check("inbox rows preserved", inbox_count == 2, f"got {inbox_count}")
    _check("2 nodes created", node_count == 2, f"got {node_count}")


def smoke_help(tmp: Path) -> None:
    print("\n[HELP] every command has --help")
    for cmd in ["init", "status", "ingest", "list", "show", "derive", "search", "render"]:
        p = _run([cmd, "--help"])
        _check(f"{cmd} --help exits 0", p.returncode == 0)
        _check(f"{cmd} --help mentions usage", "Usage:" in p.stdout, f"got: {p.stdout[:80]}")


def smoke_full_e2e(tmp: Path) -> None:
    """One continuous flow: inbox → derive → search."""
    print("\n[E2E FLOW] ingest inbox → derive → search → verify")
    db, vault = _fresh_store(tmp, "e2e")

    export = """\
[01/06/2024, 09:00:00] Alice: https://example.com/alpha
[01/06/2024, 09:01:00] Bob: https://example.com/beta
"""
    inbox_path = tmp / "inbox.txt"
    inbox_path.write_text(export, encoding="utf-8")

    _run(["ingest", "--db", str(db), "--vault", str(vault), "--inbox", str(inbox_path)],
         env={"MEMEX_FETCHER_MODULE": FAKE_FETCHER})

    p = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list", p)
    l0_ids = [r["id"] for r in lst]
    _check("2 L0 nodes from inbox", len(l0_ids) == 2, f"got {len(l0_ids)}")

    for l0_id in l0_ids:
        _run(["derive", "--db", str(db), "--vault", str(vault), l0_id],
             env={"MEMEX_LLM_MODULE": FAKE_LLM})

    p = _run(["list", "--db", str(db), "--vault", str(vault)])
    lst = _expect_json("list", p)
    _check("4 nodes total (2 l0 + 2 derivations)", len(lst) == 4, f"got {len(lst)}")

    p = _run(["search", "--db", str(db), "--vault", str(vault), "broader pattern"])
    res = _expect_json("search", p)
    _check("search finds both derivations", len(res) == 2, f"got {len(res)}")


# ── runner ──────────────────────────────────────────────────────


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        smoke_lifecycle(tmp)
        smoke_idempotency(tmp)
        smoke_fetch_failure(tmp)
        smoke_inbox(tmp)
        smoke_inbox_with_failures(tmp)
        smoke_pending(tmp)
        smoke_derive_passing(tmp)
        smoke_derive_failing(tmp)
        smoke_derive_idempotent(tmp)
        smoke_derive_all(tmp)
        smoke_search(tmp)
        smoke_errors(tmp)
        smoke_migration(tmp)
        smoke_youtube(tmp)
        smoke_l0_immutable(tmp)
        smoke_render(tmp)
        smoke_from_inbox(tmp)
        smoke_capture(tmp)
        smoke_help(tmp)
        smoke_full_e2e(tmp)

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