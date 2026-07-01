"""Store — SQLite persistence for memex.

Deep module: hides connection lifecycle, raw SQL, schema migration,
and row marshalling behind a small domain interface.

ADR-0008 boundary: SQLite owns structure (Store), markdown owns content (CLI / Vault).
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StoreError(Exception):
    """Wraps sqlite3 errors from Store operations."""


_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS node (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    tier         TEXT,
    trust_state  TEXT NOT NULL CHECK (trust_state IN ('draft','auto-verified','human-approved','stale')),
    depth        INTEGER NOT NULL,
    content_path TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source (
    node_id       TEXT PRIMARY KEY REFERENCES node(id),
    canonical_key TEXT NOT NULL UNIQUE,
    source_url    TEXT NOT NULL,
    title         TEXT,
    fetched_at    TEXT,
    failed        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edge (
    id        TEXT PRIMARY KEY,
    type      TEXT NOT NULL CHECK (type IN ('provenance','association')),
    relation  TEXT NOT NULL CHECK (relation IN ('derived_from','related','contradicts','refines')),
    from_node TEXT NOT NULL REFERENCES node(id),
    to_node   TEXT NOT NULL REFERENCES node(id)
);

CREATE TABLE IF NOT EXISTS cursor (
    source_name TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


class Store:
    """SQLite-backed persistence for Nodes, Sources, and the Ledger.

    Two entry points:
        store = Store(conn)           # for in-memory tests
        with Store.open(path) as s:   # for CLI (auto-commit/rollback/close)
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._con = conn
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA foreign_keys = ON")

    @classmethod
    @contextmanager
    def open(cls, path: str | Path) -> Iterator[Store]:
        """Open file-backed store. Commit on success, rollback on error."""
        con = sqlite3.connect(str(path))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield cls(con)
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    # ── Schema ────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Create all tables (idempotent) and apply pending migrations."""
        self._con.executescript(_SCHEMA_SQL)
        try:
            self._con.execute("ALTER TABLE source ADD COLUMN failed INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists

    # ── Ledger ────────────────────────────────────────────────────

    def lookup_by_canonical_key(self, ckey: str) -> dict[str, Any] | None:
        """Check the ledger for an existing canonical key.

        Returns ``{node_id, failed}`` or ``None``.
        """
        row = self._con.execute(
            "SELECT node_id, failed FROM source WHERE canonical_key = ?",
            (ckey,),
        ).fetchone()
        if row is None:
            return None
        return {"node_id": row["node_id"], "failed": bool(row["failed"])}

    # ── Nodes ─────────────────────────────────────────────────────

    def create_node(
        self,
        *,
        node_id: str,
        kind: str,
        tier: str | None = None,
        trust_state: str = "draft",
        depth: int = 0,
        content_path: str = "",
        created_at: str | None = None,
    ) -> None:
        """Insert a node row. ``created_at`` defaults to now (UTC ISO)."""
        if created_at is None:
            created_at = datetime.now(timezone.utc).isoformat()
        try:
            self._con.execute(
                """
                INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, kind, tier, trust_state, depth, content_path, created_at),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    # ── Sources ───────────────────────────────────────────────────

    def attach_source(
        self,
        *,
        node_id: str,
        canonical_key: str,
        source_url: str,
        title: str | None = None,
        fetched_at: str | None = None,
        failed: bool = False,
    ) -> None:
        """Insert a source row linked to an existing node."""
        try:
            self._con.execute(
                """
                INSERT INTO source (node_id, canonical_key, source_url, title, fetched_at, failed)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (node_id, canonical_key, source_url, title, fetched_at, 1 if failed else 0),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    # ── Reads ─────────────────────────────────────────────────────

    def list_nodes(self) -> list[dict[str, Any]]:
        """All nodes with their source info, ordered by created_at."""
        rows = self._con.execute(
            """
            SELECT n.id, n.kind, n.tier, n.trust_state, s.canonical_key
            FROM node n
            LEFT JOIN source s ON s.node_id = n.id
            ORDER BY n.created_at
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Full node + source by id.

        Returns ``{id, kind, tier, trust_state, depth, content_path, created_at,
        canonical_key, source_url, title, fetched_at, failed}`` or ``None``.
        """
        row = self._con.execute(
            """
            SELECT
                n.id, n.kind, n.tier, n.trust_state, n.depth, n.content_path, n.created_at,
                s.canonical_key, s.source_url, s.title, s.fetched_at, s.failed
            FROM node n
            LEFT JOIN source s ON s.node_id = n.id
            WHERE n.id = ?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("failed") is not None:
            d["failed"] = bool(d["failed"])
        return d

    # ── Connection ────────────────────────────────────────────────

    def close(self) -> None:
        self._con.close()

    # ── Edges (stubs — future) ─────────────────────────────────────

    def create_edge(self, *, type: str, relation: str, from_node: str, to_node: str) -> str:
        raise NotImplementedError

    def list_edges(self, *, node_id: str | None = None, type: str | None = None,
                   relation: str | None = None) -> list[dict]:
        raise NotImplementedError

    # ── Cursors (stubs — future) ────────────────────────────────────

    def get_cursor(self, source_name: str) -> str | None:
        raise NotImplementedError

    def set_cursor(self, source_name: str, value: str) -> None:
        raise NotImplementedError
