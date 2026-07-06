"""Store — SQLite persistence for memex.

Deep module: hides connection lifecycle, raw SQL, schema migration,
and row marshalling behind a small domain interface.

ADR-0008 boundary: SQLite owns structure (Store), markdown owns content (CLI / Vault).
"""
from __future__ import annotations

import json
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

CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    url         TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    note        TEXT,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type     TEXT NOT NULL CHECK (event_type IN ('contradicts_edge_needs_review')),
    edge_id        TEXT NOT NULL REFERENCES edge(id),
    target_node_id TEXT NOT NULL REFERENCES node(id),
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('pending','closed')) DEFAULT 'pending',
    closed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_queue_status ON event_queue(status);
CREATE INDEX IF NOT EXISTS idx_event_queue_target ON event_queue(target_node_id);

CREATE TABLE IF NOT EXISTS event_node_link (
    event_id     INTEGER NOT NULL REFERENCES event_queue(id),
    node_id      TEXT NOT NULL REFERENCES node(id),
    contested_at TEXT NOT NULL,
    PRIMARY KEY (event_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_event_node_link_node ON event_node_link(node_id);
CREATE INDEX IF NOT EXISTS idx_event_node_link_event ON event_node_link(event_id);

CREATE TABLE IF NOT EXISTS review_proposal (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id              INTEGER NOT NULL UNIQUE REFERENCES event_queue(id),
    affected_node_ids     TEXT NOT NULL,
    damage_boundary_node_id TEXT REFERENCES node(id),
    rationale_md          TEXT NOT NULL,
    confidence            TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    status                TEXT NOT NULL CHECK (status IN ('pending','accepted','rejected','dismissed')) DEFAULT 'pending',
    human_note            TEXT,
    created_at            TEXT NOT NULL,
    resolved_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_proposal_status ON review_proposal(status);
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
        try:
            self._con.execute("ALTER TABLE node ADD COLUMN check_failures TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._con.execute("ALTER TABLE node ADD COLUMN is_contested INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._con.execute("ALTER TABLE node ADD COLUMN contested_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._con.execute(
                "ALTER TABLE edge ADD COLUMN written_by TEXT NOT NULL DEFAULT 'human'"
                " CHECK (written_by IN ('human','llm','check','system'))"
            )
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
        """All nodes with full metadata, ordered by created_at.

        Returns the same per-node fields as ``get_node``: ``{id, kind, tier,
        trust_state, depth, content_path, created_at, check_failures,
        is_contested, contested_at,
        canonical_key, source_url, title, fetched_at, failed}``.
        """
        rows = self._con.execute(
            """
            SELECT
                n.id, n.kind, n.tier, n.trust_state, n.depth,
                n.content_path, n.created_at, n.check_failures,
                n.is_contested, n.contested_at,
                s.canonical_key, s.source_url, s.title, s.fetched_at, s.failed
            FROM node n
            LEFT JOIN source s ON s.node_id = n.id
            ORDER BY n.created_at
            """
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("failed") is not None:
                d["failed"] = bool(d["failed"])
            cf_json = d.pop("check_failures", None)
            if cf_json is not None:
                d["check_failures"] = json.loads(cf_json)
            else:
                d["check_failures"] = None
            # is_contested and contested_at are plain int/TEXT — no JSON decoding needed
            d["is_contested"] = bool(d["is_contested"])
            result.append(d)
        return result

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Full node + source by id.

        Returns ``{id, kind, tier, trust_state, depth, content_path, created_at,
        check_failures, is_contested, contested_at,
        canonical_key, source_url, title, fetched_at, failed}`` or ``None``.
        """
        row = self._con.execute(
            """
            SELECT
                n.id, n.kind, n.tier, n.trust_state, n.depth, n.content_path, n.created_at,
                n.check_failures,
                n.is_contested, n.contested_at,
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
        # Decode check_failures: present (even if empty list) for derivation nodes;
        # None for L0 nodes that have never been checked.
        cf_json = d.pop("check_failures", None)
        if cf_json is not None:
            d["check_failures"] = json.loads(cf_json)
        else:
            d["check_failures"] = None
        # is_contested and contested_at are plain int/TEXT — no JSON decoding needed
        d["is_contested"] = bool(d["is_contested"])
        return d

    # ── Connection ────────────────────────────────────────────────

    def close(self) -> None:
        self._con.close()

    # ── Edges ──────────────────────────────────────────────────────

    def create_edge(self, *, edge_id: str, type: str, relation: str,
                    from_node: str, to_node: str,
                    written_by: str = "human") -> None:
        """Insert a typed edge between two nodes.

        When ``relation == 'contradicts'`` the contested-state propagation
        flow is triggered automatically within the current transaction.
        """
        try:
            self._con.execute(
                """
                INSERT INTO edge (id, type, relation, from_node, to_node, written_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (edge_id, type, relation, from_node, to_node, written_by),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

        if relation == "contradicts":
            self._propagate_contradiction(edge_id, to_node)

    # ── Contestation propagation (internal) ────────────────────────

    def _propagate_contradiction(self, edge_id: str, target_node_id: str) -> None:
        """Open a contestation event, walk provenance descendants,
        link each descendant and the target node, and flag
        previously-uncontested nodes.

        This entire sequence shares the caller's transaction — no commit here.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            descendants = self.find_provenance_descendants(target_node_id)
            event_id = self.open_contestation_event(
                edge_id=edge_id,
                target_node_id=target_node_id,
            )
            all_nodes = [target_node_id] + descendants
            for node_id in all_nodes:
                self.link_event_to_node(event_id, node_id, now)
                # Only flag nodes that are not already contested
                self._con.execute(
                    "UPDATE node SET is_contested = 1, contested_at = ? WHERE id = ? AND is_contested = 0",
                    (now, node_id),
                )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def find_provenance_descendants(self, target_node_id: str) -> list[str]:
        """Walk ``derived_from`` edges transitively to find all nodes
        that depend on ``target_node_id``.

        Returns node ids, empty list when none exist.
        """
        try:
            rows = self._con.execute(
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
                (target_node_id,),
            ).fetchall()
            return [r["id"] for r in rows]
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def open_contestation_event(self, edge_id: str, target_node_id: str) -> int:
        """Insert a new contestation event and return its id."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            cur = self._con.execute(
                """
                INSERT INTO event_queue (event_type, edge_id, target_node_id, created_at, status)
                VALUES ('contradicts_edge_needs_review', ?, ?, ?, 'pending')
                """,
                (edge_id, target_node_id, now),
            )
            return cur.lastrowid
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    # ── Review proposals ──────────────────────────────────────────

    def get_pending_events_without_proposal(self) -> list[dict]:
        """Return all pending event_queue rows that have no review_proposal."""
        try:
            rows = self._con.execute(
                """
                SELECT eq.* FROM event_queue eq
                LEFT JOIN review_proposal rp ON rp.event_id = eq.id
                WHERE eq.status = 'pending'
                  AND rp.id IS NULL
                ORDER BY eq.created_at
                """
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def write_review_proposal(
        self,
        *,
        event_id: int,
        affected_node_ids: list[str],
        damage_boundary_node_id: str | None = None,
        rationale_md: str,
        confidence: str,
    ) -> int:
        """Insert a review proposal and return its id.

        ``affected_node_ids`` is JSON-serialized internally.
        Raises ``StoreError`` on UNIQUE violation (duplicate event_id).
        """
        now = datetime.now(timezone.utc).isoformat()
        affected_json = json.dumps(affected_node_ids)
        try:
            cur = self._con.execute(
                """
                INSERT INTO review_proposal
                    (event_id, affected_node_ids, damage_boundary_node_id,
                     rationale_md, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, affected_json, damage_boundary_node_id,
                 rationale_md, confidence, now),
            )
            return cur.lastrowid
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def get_review_queue(self) -> list[dict]:
        """Return pending events without proposals AND pending proposals,
        each annotated with a ``kind`` field.
        """
        try:
            # Pending events without a proposal
            events = self._con.execute(
                """
                SELECT eq.*, 'pending_event' AS kind
                FROM event_queue eq
                LEFT JOIN review_proposal rp ON rp.event_id = eq.id
                WHERE eq.status = 'pending'
                  AND rp.id IS NULL
                """
            ).fetchall()
            # Pending proposals joined with their event
            proposals = self._con.execute(
                """
                SELECT rp.id, rp.event_id, rp.affected_node_ids,
                       rp.damage_boundary_node_id, rp.rationale_md,
                       rp.confidence, rp.status, rp.human_note,
                       rp.created_at, rp.resolved_at,
                       eq.event_type, eq.edge_id, eq.target_node_id,
                       'pending_proposal' AS kind
                FROM review_proposal rp
                JOIN event_queue eq ON eq.id = rp.event_id
                WHERE rp.status = 'pending'
                """
            ).fetchall()
            combined = [dict(r) for r in events] + [dict(r) for r in proposals]
            combined.sort(key=lambda x: x["created_at"])
            return combined
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def link_event_to_node(self, event_id: int, node_id: str, contested_at: str) -> None:
        """Link an event to a contested node."""
        try:
            self._con.execute(
                "INSERT INTO event_node_link (event_id, node_id, contested_at) VALUES (?, ?, ?)",
                (event_id, node_id, contested_at),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    # ── Adjudication (accept / reject / dismiss) ──────────────────

    def _close_contestation_event(self, event_id: int) -> list[str]:
        """Close an event's links and recompute is_contested for linked nodes.

        1. Find all nodes linked to this event.
        2. Delete the links.
        3. For each formerly-linked node, if it has no other pending event,
           clear is_contested.
        4. Return the list of formerly-linked node ids.
        """
        try:
            # 1. Find linked nodes
            linked = self._con.execute(
                "SELECT node_id FROM event_node_link WHERE event_id = ?",
                (event_id,),
            ).fetchall()
            node_ids = [r["node_id"] for r in linked]

            # 2. Delete links
            self._con.execute(
                "DELETE FROM event_node_link WHERE event_id = ?",
                (event_id,),
            )

            # 3. Recompute is_contested for each node
            for node_id in node_ids:
                other = self._con.execute(
                    """
                    SELECT 1 FROM event_node_link enl
                    JOIN event_queue eq ON eq.id = enl.event_id
                    WHERE enl.node_id = ?
                      AND eq.status = 'pending'
                      AND enl.event_id != ?
                    LIMIT 1
                    """,
                    (node_id, event_id),
                ).fetchone()
                if other is None:
                    self._con.execute(
                        "UPDATE node SET is_contested = 0, contested_at = NULL WHERE id = ?",
                        (node_id,),
                    )

            return node_ids
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def accept_proposal(self, proposal_id: int, human_note: str | None = None) -> dict:
        """Accept a review proposal — mark affected nodes as stale, close event.

        Returns status dict. Idempotent — second call returns already_resolved.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            row = self._con.execute(
                "SELECT event_id, status, affected_node_ids FROM review_proposal WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                return {"status": "not_found", "proposal_id": proposal_id}
            if row["status"] != "pending":
                return {"status": "already_resolved", "proposal_id": proposal_id, "current_status": row["status"]}

            event_id = row["event_id"]
            affected_node_ids = json.loads(row["affected_node_ids"])

            # 4. Set affected nodes to stale
            for node_id in affected_node_ids:
                self._con.execute(
                    "UPDATE node SET trust_state = 'stale' WHERE id = ?",
                    (node_id,),
                )

            # 5. Close contestation event links
            formerly_linked = self._close_contestation_event(event_id)

            # 6. Update proposal
            self._con.execute(
                "UPDATE review_proposal SET status = 'accepted', resolved_at = ?, human_note = ? WHERE id = ?",
                (now, human_note, proposal_id),
            )

            # 7. Close event
            self._con.execute(
                "UPDATE event_queue SET status = 'closed', closed_at = ? WHERE id = ?",
                (now, event_id),
            )

            # Compute still_contested: intersection of formerly-linked nodes
            # that remain is_contested=1 after cleanup
            still_contested = []
            for nid in formerly_linked:
                node = self.get_node(nid)
                if node and node["is_contested"]:
                    still_contested.append(nid)

            return {
                "status": "accepted",
                "proposal_id": proposal_id,
                "affected": affected_node_ids,
                "still_contested": still_contested,
            }
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def reject_proposal(self, proposal_id: int, human_note: str | None = None) -> dict:
        """Reject a review proposal — close event, no trust_state changes.

        Returns status dict. Idempotent.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            row = self._con.execute(
                "SELECT event_id, status FROM review_proposal WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                return {"status": "not_found", "proposal_id": proposal_id}
            if row["status"] != "pending":
                return {"status": "already_resolved", "proposal_id": proposal_id, "current_status": row["status"]}

            event_id = row["event_id"]
            uncontested = self._close_contestation_event(event_id)

            self._con.execute(
                "UPDATE review_proposal SET status = 'rejected', resolved_at = ?, human_note = ? WHERE id = ?",
                (now, human_note, proposal_id),
            )
            self._con.execute(
                "UPDATE event_queue SET status = 'closed', closed_at = ? WHERE id = ?",
                (now, event_id),
            )

            return {
                "status": "rejected",
                "proposal_id": proposal_id,
                "uncontested": uncontested,
            }
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def dismiss_proposal(self, proposal_id: int, human_note: str | None = None) -> dict:
        """Dismiss a review proposal — close event, no trust_state changes.

        Identical to reject except status='dismissed'.
        Returns status dict. Idempotent.
        """
        try:
            now = datetime.now(timezone.utc).isoformat()
            row = self._con.execute(
                "SELECT event_id, status FROM review_proposal WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                return {"status": "not_found", "proposal_id": proposal_id}
            if row["status"] != "pending":
                return {"status": "already_resolved", "proposal_id": proposal_id, "current_status": row["status"]}

            event_id = row["event_id"]
            uncontested = self._close_contestation_event(event_id)

            self._con.execute(
                "UPDATE review_proposal SET status = 'dismissed', resolved_at = ?, human_note = ? WHERE id = ?",
                (now, human_note, proposal_id),
            )
            self._con.execute(
                "UPDATE event_queue SET status = 'closed', closed_at = ? WHERE id = ?",
                (now, event_id),
            )

            return {
                "status": "dismissed",
                "proposal_id": proposal_id,
                "uncontested": uncontested,
            }
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def get_node_open_events(self, node_id: str) -> list[int]:
        """Return event_ids of all pending events that cover ``node_id``."""
        try:
            rows = self._con.execute(
                """
                SELECT enl.event_id
                FROM event_node_link enl
                JOIN event_queue eq ON eq.id = enl.event_id
                WHERE enl.node_id = ?
                  AND eq.status = 'pending'
                """,
                (node_id,),
            ).fetchall()
            return [r["event_id"] for r in rows]
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def list_edges(self, *, node_id: str | None = None, type: str | None = None,
                   relation: str | None = None) -> list[dict]:
        """List edges, optionally filtered. node_id matches from_node or to_node."""
        clauses, params = [], []
        if node_id is not None:
            clauses.append("(from_node = ? OR to_node = ?)")
            params.extend([node_id, node_id])
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if relation is not None:
            clauses.append("relation = ?")
            params.append(relation)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._con.execute(
            f"SELECT id, type, relation, from_node, to_node, written_by FROM edge {where}", params
        ).fetchall()
        return [dict(r) for r in rows]

    def find_derived_from(self, l0_node_id: str) -> dict | None:
        """Return the first derivation node with a derived_from edge to ``l0_node_id``."""
        row = self._con.execute(
            """
            SELECT e.from_node FROM edge e
            WHERE e.to_node = ? AND e.type = 'provenance' AND e.relation = 'derived_from'
            LIMIT 1
            """,
            (l0_node_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    # ── Cursors ────────────────────────────────────────────────────

    def get_cursor(self, source_name: str) -> str | None:
        row = self._con.execute(
            "SELECT value FROM cursor WHERE source_name = ?", (source_name,)
        ).fetchone()
        return row["value"] if row else None

    def set_cursor(self, source_name: str, value: str) -> None:
        self._con.execute(
            "INSERT OR REPLACE INTO cursor (source_name, value) VALUES (?, ?)",
            (source_name, value),
        )

    # ── Inbox ──────────────────────────────────────────────────────

    def add_inbox_item(self, *, source_name: str, url: str, timestamp: str,
                       note: str | None, captured_at: str) -> None:
        """Persist a captured item to the inbox table."""
        try:
            self._con.execute(
                """
                INSERT INTO inbox (source_name, url, timestamp, note, captured_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (source_name, url, timestamp, note, captured_at),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def list_inbox(self) -> list[dict]:
        """All inbox rows."""
        rows = self._con.execute(
            "SELECT id, source_name, url, timestamp, note, captured_at FROM inbox ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_ingested_canonical_keys(self) -> set[str]:
        """All canonical keys present in the ledger (source table)."""
        rows = self._con.execute("SELECT canonical_key FROM source").fetchall()
        return {r["canonical_key"] for r in rows}

    # ── Trust state + check failures ───────────────────────────────

    def update_trust_state(
        self, *, node_id: str, trust_state: str, check_failures: list[str] | None = None
    ) -> None:
        """Set trust_state and (optionally) check_failures JSON for a node."""
        failures_json = json.dumps(check_failures) if check_failures is not None else None
        self._con.execute(
            "UPDATE node SET trust_state = ?, check_failures = ? WHERE id = ?",
            (trust_state, failures_json, node_id),
        )

