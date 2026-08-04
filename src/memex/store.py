"""Store — SQLite persistence for memex.

Deep module: hides connection lifecycle, raw SQL, schema migration,
and row marshalling behind a small domain interface.

ADR-0008 boundary: SQLite owns structure (Store), markdown owns content (CLI / Vault).
"""
from __future__ import annotations

import json
import uuid

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memex.rules import CONFIDENCE_RULES, EXTRACTED_CONFIDENCE


class StoreError(Exception):
    """Wraps sqlite3 errors from Store operations."""


_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS node (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    tier         TEXT,
    trust_state  TEXT CHECK (trust_state IN ('draft','auto-verified','human-approved','stale')),
    depth        INTEGER NOT NULL,
    content_path TEXT,
    created_at   TEXT NOT NULL,
    fetcher_type TEXT
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

CREATE TABLE IF NOT EXISTS node_idea (
    id         TEXT PRIMARY KEY,
    node_id    TEXT NOT NULL REFERENCES node(id),
    idea_text  TEXT NOT NULL,
    rank       INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_idea_node ON node_idea(node_id);
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
            store = cls(con)
            store._migrate_if_needed()
            yield store
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _migrate_if_needed(self) -> None:
        """Apply pending schema migrations when the DB exists but is behind.

        Fresh/empty DBs (no ``node`` table) are left alone — ``memex init``
        owns schema creation. Existing DBs missing the post-#95
        ``fetcher_type`` column are migrated in place (idempotent and
        transactional via ``init_schema``). Without this, a pre-#95 DB that
        arrives via git sync (ADR-0015) crashes every read command with
        ``no such column: n.fetcher_type``.
        """
        row = self._con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'node'"
        ).fetchone()
        if row is None:
            return
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(node)")}
        if "fetcher_type" not in cols:
            self.init_schema()

    # ── Schema ────────────────────────────────────────────────────

    def init_schema(self) -> None:
        """Create all tables (idempotent) and apply pending migrations.

        The whole migration — column ALTERs, the node-table rebuild, and
        the confidence backfill — runs inside a single transaction, so a
        failure in any step rolls everything back: the destructive node
        rebuild can never be left committed while later steps are
        half-applied. ``PRAGMA foreign_keys`` is a no-op inside a
        transaction, so it is toggled here (in autocommit, right after
        the schema script) around the rebuild and restored afterwards.
        Every step is idempotent, so re-running ``init_schema`` after a
        failure recovers.
        """
        self._con.executescript(_SCHEMA_SQL)
        fk_was_on = bool(self._con.execute("PRAGMA foreign_keys").fetchone()[0])
        if fk_was_on:
            self._con.execute("PRAGMA foreign_keys = OFF")
        try:
            self._con.execute("BEGIN")
            try:
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
                try:
                    self._con.execute(
                        "ALTER TABLE node ADD COLUMN confidence TEXT CHECK (confidence IN ('high','medium','low'))"
                )
                except sqlite3.OperationalError:
                    pass  # column already exists
                try:
                    self._con.execute(
                        "ALTER TABLE node ADD COLUMN synthesis_statements TEXT"
                    )
                except sqlite3.OperationalError:
                    pass  # column already exists
                self._rebuild_node_table_for_url_kind()
                self._backfill_confidence()
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            else:
                self._con.execute("COMMIT")
        finally:
            if fk_was_on:
                self._con.execute("PRAGMA foreign_keys = ON")

    def _rebuild_node_table_for_url_kind(self) -> None:
        """Rebuild ``node`` so trust_state/content_path are nullable + fetcher_type exists.

        SQLite cannot drop a NOT NULL constraint via ALTER TABLE, so DBs
        created before ticket #95 (which declared ``trust_state`` and
        ``content_path`` NOT NULL) need a full table rebuild — the standard
        12-step procedure: disable FK, create ``node_new`` with the
        desired schema, copy every existing column, drop the old table,
        rename, re-enable FK, verify ``foreign_key_check``.

        The guard above returns early when ``node`` already has the
        post-#95 shape — nullable ``trust_state``/``content_path`` plus the
        ``fetcher_type`` column, which fresh DBs get directly from
        ``_SCHEMA_SQL``. So the rebuild is a genuine one-time migration for
        pre-#95 DBs only. The column definitions below must match
        ``_SCHEMA_SQL`` plus the columns added by the ALTER TABLE
        migrations above.

        Runs inside ``init_schema``'s migration transaction with
        ``foreign_keys`` already disabled (the pragma is a no-op inside a
        transaction), so this method only issues DDL and never commits on
        its own — a failure in a later migration step rolls the rebuild
        back too, and re-running ``init_schema`` recovers.
        """
        info = self._con.execute("PRAGMA table_info(node)").fetchall()
        existing = {r["name"]: r for r in info}
        trust_notnull = bool(existing["trust_state"]["notnull"])
        content_notnull = bool(existing["content_path"]["notnull"])
        if not trust_notnull and not content_notnull and "fetcher_type" in existing:
            return

        self._con.execute(
            """
            CREATE TABLE node_new (
                id           TEXT PRIMARY KEY,
                kind         TEXT NOT NULL,
                tier         TEXT,
                trust_state  TEXT CHECK (trust_state IN ('draft','auto-verified','human-approved','stale')),
                depth        INTEGER NOT NULL,
                content_path TEXT,
                created_at   TEXT NOT NULL,
                check_failures       TEXT,
                is_contested         INTEGER NOT NULL DEFAULT 0,
                contested_at         TEXT,
                confidence           TEXT CHECK (confidence IN ('high','medium','low')),
                synthesis_statements TEXT,
                fetcher_type         TEXT
            )
            """
        )
        copy_cols = [name for name in (
            "id", "kind", "tier", "trust_state", "depth", "content_path",
            "created_at", "check_failures", "is_contested", "contested_at",
            "confidence", "synthesis_statements", "fetcher_type",
        ) if name in existing]
        collist = ", ".join(copy_cols)
        self._con.execute(
            f"INSERT INTO node_new ({collist}) SELECT {collist} FROM node"
        )
        self._con.execute("DROP TABLE node")
        self._con.execute("ALTER TABLE node_new RENAME TO node")

        violations = self._con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise StoreError(
                f"foreign key violations after node table rebuild: {violations!r}"
            )

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
        trust_state: str | None = "draft",
        depth: int = 0,
        content_path: str | None = "",
        created_at: str | None = None,
        confidence: str | None = None,
        synthesis_statements: list[str] | None = None,
        fetcher_type: str | None = None,
        derived_from: str | None = None,
    ) -> None:
        """Insert a node row. ``created_at`` defaults to now (UTC ISO).

        When ``confidence`` is omitted it is computed automatically: URL
        nodes have none (NULL), extracted nodes follow the fetcher_type map
        (``rules.EXTRACTED_CONFIDENCE``), everything else defaults to 'low'
        for fresh nodes with no edges yet.

        Kind invariants (ticket #95):

        - ``kind='url'`` — the root of every chain, zero content. tier,
          trust_state, confidence, content_path, synthesis_statements and
          fetcher_type are always stored NULL and depth is 0, regardless
          of the arguments passed.
        - ``kind='extracted'`` — tier='extracted', depth=1, content_path
          pointing to a file, plus a provenance ``derived_from`` edge to a
          URL node. ``content_path`` and ``derived_from`` are required and
          ``derived_from`` must reference an existing ``kind='url'`` node.

        ``synthesis_statements`` is persisted as JSON for structured querying.
        """
        if kind == "url":
            tier, trust_state, depth, content_path, confidence = None, None, 0, None, None
            # Zero-content invariant: LLM-derived statements and fetcher
            # metadata never belong on a URL node, whatever the caller passed.
            synthesis_statements = None
            fetcher_type = None
        elif kind == "extracted":
            tier, depth = "extracted", 1
            if not content_path:
                raise ValueError("extracted nodes require a content_path")
            if not derived_from:
                raise ValueError("extracted nodes require a derived_from URL node")
            parent = self._con.execute(
                "SELECT kind FROM node WHERE id = ?", (derived_from,)
            ).fetchone()
            if parent is None or parent["kind"] != "url":
                raise ValueError("extracted nodes must derive from a URL node")
            if confidence is None:
                confidence = EXTRACTED_CONFIDENCE.get(fetcher_type)
        if created_at is None:
            created_at = datetime.now(timezone.utc).isoformat()
        if confidence is None and kind not in ("url", "extracted"):
            confidence = "low"  # default for fresh nodes with no edges yet
        synth_json = json.dumps(synthesis_statements) if synthesis_statements else None
        try:
            self._con.execute(
                """
                INSERT INTO node (
                    id, kind, tier, trust_state, depth, content_path, created_at,
                    confidence, synthesis_statements, fetcher_type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id, kind, tier, trust_state, depth,
                    content_path, created_at, confidence, synth_json, fetcher_type,
                ),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

        if kind == "extracted":
            self.create_edge(
                edge_id=str(uuid.uuid4()),
                type="provenance",
                relation="derived_from",
                from_node=node_id,
                to_node=derived_from,
            )

    def _backfill_confidence(self) -> None:
        """Set confidence for nodes created before the column existed.

        L0 (no tier, no provenance edge) → low.
        Notes tier → medium (1 parent after creation).
        Synthesis tier → min(parents' confidence), or low when unresolvable.
        """
        # L0 nodes: no tier → low (URL nodes have no confidence — never set)
        self._con.execute(
            "UPDATE node SET confidence = 'low' WHERE confidence IS NULL AND tier IS NULL AND kind != 'url'"
        )
        # Notes tier → medium
        self._con.execute(
            "UPDATE node SET confidence = 'medium' WHERE confidence IS NULL AND tier = 'notes'"
        )
        # Synthesis tier: min of parents' confidence
        rows = self._con.execute(
            "SELECT n.id FROM node n WHERE n.confidence IS NULL AND n.tier = 'synthesis'"
        ).fetchall()
        for row in rows:
            nid = row["id"]
            parents = self._con.execute(
                """
                SELECT n2.confidence FROM edge e
                JOIN node n2 ON n2.id = e.to_node
                WHERE e.from_node = ? AND e.type = 'provenance' AND e.relation = 'derived_from'
                """,
                (nid,),
            ).fetchall()
            if parents:
                confidences = [p["confidence"] for p in parents if p["confidence"]]
                if "low" in confidences:
                    min_c = "low"
                elif "medium" in confidences:
                    min_c = "medium"
                else:
                    min_c = "high"
            else:
                min_c = "low"
            self._con.execute(
                "UPDATE node SET confidence = ? WHERE id = ?", (min_c, nid)
            )

    def compute_node_confidence(self, node_id: str) -> str | None:
        """Compute confidence score for a node.

        URL nodes have no confidence (None). Extracted nodes take their
        confidence from the fetcher_type map (``rules.EXTRACTED_CONFIDENCE``),
        unless a ``contradicts`` edge targets them — C4 (incoming contradicts
        overrides everything) is evaluated first and wins, matching the 'low'
        that ``_propagate_contradiction`` writes into the row.
        Everything else evaluates ``CONFIDENCE_RULES`` in priority order;
        first match wins. See ``src/memex/rules.py`` for the rule
        definitions (C1–C4).

        Raises ``ValueError`` if ``node_id`` is not found.
        """
        node = self.get_node(node_id)
        if node is None:
            raise ValueError(f"node not found: {node_id}")

        if node["kind"] == "url":
            return None
        if node["kind"] == "extracted":
            # C4 is first in priority order: an incoming contradicts edge
            # overrides everything, fetcher map included.
            c4 = next(rule for rule in CONFIDENCE_RULES if rule.id == "C4")
            if c4.condition(self, node_id):
                return c4.consequence
            return EXTRACTED_CONFIDENCE.get(node.get("fetcher_type"))

        for rule in CONFIDENCE_RULES:
            if rule.condition(self, node_id):
                return rule.consequence
        return "low"  # fallback

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

    def list_nodes(
        self,
        *,
        kind: str | None = None,
        tier: str | None = None,
        trust_state: str | None = None,
        confidence: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """All nodes with full metadata, ordered by created_at.

        Optional filters: kind, tier, trust_state, confidence, limit, offset.

        ``kind`` defaults to excluding URL nodes (they are roots, not viewing
        surfaces); pass ``kind='url'`` to include them. The exclusion happens
        in the WHERE clause so limit/offset pagination stays correct.

        Returns the same per-node fields as ``get_node``: ``{id, kind, tier,
        trust_state, depth, content_path, created_at, confidence, check_failures,
        synthesis_statements, is_contested, contested_at, fetcher_type,
        canonical_key, source_url, title, fetched_at, failed}``.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if kind is None:
            clauses.append("n.kind != 'url'")
        else:
            clauses.append("n.kind = ?")
            params.append(kind)
        if tier is not None:
            clauses.append("n.tier = ?")
            params.append(tier)
        if trust_state is not None:
            clauses.append("n.trust_state = ?")
            params.append(trust_state)
        if confidence is not None:
            clauses.append("n.confidence = ?")
            params.append(confidence)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT
                n.id, n.kind, n.tier, n.trust_state, n.depth,
                n.content_path, n.created_at, n.check_failures,
                n.synthesis_statements,
                n.is_contested, n.contested_at, n.confidence, n.fetcher_type,
                s.canonical_key, s.source_url, s.title, s.fetched_at, s.failed
            FROM node n
            LEFT JOIN source s ON s.node_id = n.id
            {where}
            ORDER BY n.created_at
        """
        if limit is not None:
            sql += f" LIMIT {limit}"
        if offset is not None:
            sql += f" OFFSET {offset}"
        rows = self._con.execute(sql, params).fetchall()
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
            ss_json = d.pop("synthesis_statements", None)
            if ss_json is not None:
                d["synthesis_statements"] = json.loads(ss_json)
            else:
                d["synthesis_statements"] = None
            # is_contested and contested_at are plain int/TEXT — no JSON decoding needed
            d["is_contested"] = bool(d["is_contested"])
            result.append(d)
        return result

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Full node + source by id.
        Returns ``{id, kind, tier, trust_state, depth, content_path, created_at,
        confidence, check_failures, synthesis_statements, is_contested, contested_at,
        fetcher_type, canonical_key, source_url, title, fetched_at, failed}`` or ``None``.
        """
        row = self._con.execute(
            """
            SELECT
                n.id, n.kind, n.tier, n.trust_state, n.depth, n.content_path, n.created_at,
                n.check_failures,
                n.synthesis_statements,
                n.is_contested, n.contested_at, n.confidence, n.fetcher_type,
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
        # Decode synthesis_statements: None for L0 nodes / nodes without an LLM pass.
        ss_json = d.pop("synthesis_statements", None)
        if ss_json is not None:
            d["synthesis_statements"] = json.loads(ss_json)
        else:
            d["synthesis_statements"] = None
        # is_contested and contested_at are plain int/TEXT — no JSON decoding needed
        d["is_contested"] = bool(d["is_contested"])
        return d


    def create_edge(self, *, edge_id: str, type: str, relation: str,
                    from_node: str, to_node: str,
                    written_by: str = "human") -> None:
        """Insert a typed edge between two nodes.

        When ``relation == 'contradicts'`` the contested-state propagation
        flow is triggered automatically within the current transaction.

        Raises ``StoreError`` when a 'contradicts' edge targets a URL node:
        URL nodes are the immutable root of every chain and can never become
        contested. The check runs before the insert, so nothing is written.
        """
        if relation == "contradicts":
            target = self._con.execute(
                "SELECT kind FROM node WHERE id = ?", (to_node,)
            ).fetchone()
            if target is not None and target["kind"] == "url":
                raise StoreError("URL nodes cannot be contested")
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
        previously-uncontested nodes. Also set target confidence to
        low and cascade confidence recomputation through descendant
        synthesis nodes.

        This entire sequence shares the caller's transaction — no commit here.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            descendants = self._find_provenance_descendants(target_node_id)
            event_id = self._open_contestation_event(
                edge_id=edge_id,
                target_node_id=target_node_id,
            )
            all_nodes = [target_node_id] + descendants
            for node_id in all_nodes:
                self._link_event_to_node(event_id, node_id, now)
                # Only flag nodes that are not already contested
                self._con.execute(
                    "UPDATE node SET is_contested = 1, contested_at = ? WHERE id = ? AND is_contested = 0",
                    (now, node_id),
                )

            # Confidence cascade: target node goes to low
            self._con.execute(
                "UPDATE node SET confidence = 'low' WHERE id = ?",
                (target_node_id,),
            )

            # Cascade: recompute confidence for descendant synthesis nodes
            # as min of their parents' confidence. Loop until stable
            # (converges in at most depth-of-graph iterations).
            changed = True
            while changed:
                changed = False
                for node_id in descendants:
                    row = self._con.execute(
                        "SELECT tier FROM node WHERE id = ?", (node_id,)
                    ).fetchone()
                    if row is None or row["tier"] != "synthesis":
                        continue
                    # Get current confidence of this node's parents
                    parents = self._con.execute(
                        """
                        SELECT n2.confidence FROM edge e
                        JOIN node n2 ON n2.id = e.to_node
                        WHERE e.from_node = ? AND e.type = 'provenance' AND e.relation = 'derived_from'
                        """,
                        (node_id,),
                    ).fetchall()
                    if not parents:
                        continue
                    confidences = [p["confidence"] for p in parents if p["confidence"]]
                    if "low" in confidences:
                        new_conf = "low"
                    elif "medium" in confidences:
                        new_conf = "medium"
                    else:
                        new_conf = "high" if confidences else "low"
                    # Update if different
                    cur = self._con.execute(
                        "UPDATE node SET confidence = ? WHERE id = ? AND confidence != ?",
                        (new_conf, node_id, new_conf),
                    )
                    if cur.rowcount > 0:
                        changed = True
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def _find_provenance_descendants(self, target_node_id: str) -> list[str]:
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

    def _open_contestation_event(self, edge_id: str, target_node_id: str) -> int:
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

    def _link_event_to_node(self, event_id: int, node_id: str, contested_at: str) -> None:
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

    def _get_node_open_events(self, node_id: str) -> list[int]:
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

    def find_extracted_child(self, url_node_id: str) -> dict | None:
        """Return the ``kind='extracted'`` child of a URL node, or None.

        Filters on the node kind explicitly (a URL node may have other
        derivation children) and orders deterministically, so the dedup
        lookup can never mistake a summary/synthesis child for the
        extracted node. Returns the full node dict (via ``get_node``).
        """
        row = self._con.execute(
            """
            SELECT e.from_node FROM edge e
            JOIN node n ON n.id = e.from_node
            WHERE e.to_node = ? AND e.type = 'provenance' AND e.relation = 'derived_from'
              AND n.kind = 'extracted'
            ORDER BY n.created_at
            LIMIT 1
            """,
            (url_node_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_node(row["from_node"])
    def find_url_parent(self, node_id: str) -> dict[str, Any] | None:
        """Return the node targeted by the first outgoing provenance derived_from edge.

        Extracted nodes carry no source row — their source_url/title live on
        the URL node referenced by their outgoing ``derived_from`` edge.
        Returns the full parent node dict (via ``get_node``) or ``None`` when
        no such edge exists.
        """
        row = self._con.execute(
            """
            SELECT to_node FROM edge
            WHERE from_node = ? AND type = 'provenance' AND relation = 'derived_from'
            LIMIT 1
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_node(row["to_node"])

    def find_synthesis_by_parents(self, parent_ids: list[str]) -> dict | None:
        """Find a synthesis node whose unordered derived_from set matches *exactly*.

        Returns the full node dict (via ``get_node``) or ``None``.
        """
        if not parent_ids:
            return None
        n = len(parent_ids)
        placeholders = ",".join("?" * n)
        row = self._con.execute(
            f"""
            SELECT e.from_node FROM edge e
            JOIN node n ON n.id = e.from_node
            WHERE e.type = 'provenance' AND e.relation = 'derived_from'
              AND n.tier = 'synthesis'
              AND e.to_node IN ({placeholders})
            GROUP BY e.from_node
            HAVING COUNT(*) = ?
               AND (SELECT COUNT(*) FROM edge e2
                    WHERE e2.from_node = e.from_node
                      AND e2.type = 'provenance' AND e2.relation = 'derived_from') = ?
            """,
            (*parent_ids, n, n),
        ).fetchone()
        if row is None:
            return None
        return self.get_node(row["from_node"])

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

    def update_extracted_fetcher(
        self, node_id: str, fetcher_type: str, content_path: str | None = None
    ) -> None:
        """Refresh an extracted node's fetcher metadata after a re-extract.

        Sets ``fetcher_type`` and recomputes ``confidence`` from the fetcher
        map so a re-extract through a different fetcher never leaves stale
        values behind (an incoming C4 ``contradicts`` edge keeps its 'low'
        override via ``compute_node_confidence``). When ``content_path`` is
        given, the node row's file location is updated too — a re-extract
        may move the node between a fetcher cache artifact
        (``vault/.cache/...``) and a CLI-owned file
        (``vault/extracted/<node>.md``), and derive/render read the row's
        content_path (ticket #99, findings 2/3).
        """
        try:
            if content_path is None:
                self._con.execute(
                    "UPDATE node SET fetcher_type = ? WHERE id = ?",
                    (fetcher_type, node_id),
                )
            else:
                self._con.execute(
                    "UPDATE node SET fetcher_type = ?, content_path = ? WHERE id = ?",
                    (fetcher_type, content_path, node_id),
                )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e
        confidence = self.compute_node_confidence(node_id)
        if confidence is not None:
            try:
                self._con.execute(
                    "UPDATE node SET confidence = ? WHERE id = ?",
                    (confidence, node_id),
                )
            except sqlite3.Error as e:
                raise StoreError(str(e)) from e

    def update_source_title(self, node_id: str, title: str) -> None:
        """Update the title of a source row."""
        try:
            self._con.execute(
                "UPDATE source SET title = ? WHERE node_id = ?",
                (title, node_id),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def mark_source_failed(self, node_id: str, fetched_at: str) -> None:
        """Record a failed fetch on a source row (``failed=1``)."""
        try:
            self._con.execute(
                "UPDATE source SET fetched_at = ?, failed = 1 WHERE node_id = ?",
                (fetched_at, node_id),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def update_source_after_fetch(
        self, node_id: str, title: str | None, fetched_at: str
    ) -> None:
        """Record a successful fetch: clear the failed flag and refresh the title."""
        try:
            self._con.execute(
                "UPDATE source SET failed = 0, fetched_at = ?, title = ? WHERE node_id = ?",
                (fetched_at, title, node_id),
            )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    # ── Ideas ─────────────────────────────────────────────────────

    def set_node_ideas(self, node_id: str, ideas: list[str]) -> None:
        """Replace all ideas for a node. Atomic (single transaction)."""
        try:
            self._con.execute("DELETE FROM node_idea WHERE node_id = ?", (node_id,))
            for rank, idea_text in enumerate(ideas, start=1):
                idea_id = str(uuid.uuid4())
                now = datetime.now(timezone.utc).isoformat()
                self._con.execute(
                    "INSERT INTO node_idea (id, node_id, idea_text, rank, created_at) VALUES (?, ?, ?, ?, ?)",
                    (idea_id, node_id, idea_text, rank, now),
                )
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def get_node_ideas(self, node_id: str) -> list[dict]:
        """Return ideas for a node, sorted by rank."""
        try:
            rows = self._con.execute(
                "SELECT id, node_id, idea_text, rank, created_at FROM node_idea WHERE node_id = ? ORDER BY rank",
                (node_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    def search_ideas(self, query: str) -> list[dict]:
        """Search ideas by text LIKE. Returns node metadata + idea details."""
        try:
            rows = self._con.execute(
                """
                SELECT ni.id AS idea_id, ni.idea_text, ni.rank AS match_rank,
                       n.id AS node_id, n.kind AS node_kind, n.tier AS node_tier
                FROM node_idea ni
                JOIN node n ON n.id = ni.node_id
                WHERE ni.idea_text LIKE ?
                ORDER BY ni.rank
                """,
                (f"%{query}%",),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    # ── Delete ────────────────────────────────────────────────────

    def delete_node(self, node_id: str, cascade: bool = False) -> dict:
        """Remove a node, its edges, source, and contestation links.

        When ``cascade=True``, also removes all provenance descendants
        transitively. Returns ``{"status": "deleted", "removed": [node_id, ...]}``.
        Returns ``{"status": "not_found"}`` when the node doesn't exist.
        """
        node = self.get_node(node_id)
        if node is None:
            return {"status": "not_found"}

        # Check for incoming edges if not cascading
        if not cascade:
            incoming = self._con.execute(
                "SELECT COUNT(*) FROM edge WHERE to_node = ?", (node_id,)
            ).fetchone()[0]
            if incoming > 0:
                return {"status": "has_dependents", "incoming_edges": incoming,
                        "detail": "Use --cascade to remove dependents"}

        removed = self._delete_node_internal(node_id, cascade)
        return {"status": "deleted", "removed": removed}

    def _delete_node_internal(
        self, node_id: str, cascade: bool, _visited: set[str] | None = None
    ) -> list[str]:
        """Delete a node and optionally its descendants. Returns list of all removed ids.

        ``_visited`` tracks ids already removed by a nested recursive call so
        each node is reported exactly once in ``removed`` (the descendant walk
        revisits nodes a deeper call has already deleted).
        """
        if _visited is None:
            _visited = set()
        if node_id in _visited:
            return []
        _visited.add(node_id)

        removed = [node_id]

        # Cascade: delete descendants first (deep-first)
        if cascade:
            descendants = self._find_provenance_descendants(node_id)
            for desc_id in descendants:
                desc_removed = self._delete_node_internal(desc_id, cascade=True, _visited=_visited)
                removed.extend(desc_removed)

        try:
            # Remove contestation event links
            open_events = self._get_node_open_events(node_id)
            for event_id in open_events:
                self._close_contestation_event(event_id)
                self._con.execute(
                    "UPDATE event_queue SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'pending'",
                    (datetime.now(timezone.utc).isoformat(), event_id),
                )
                self._con.execute(
                    "UPDATE review_proposal SET status = 'dismissed', resolved_at = ? WHERE event_id = ? AND status = 'pending'",
                    (datetime.now(timezone.utc).isoformat(), event_id),
                )

            # Remove edges (both directions)
            self._con.execute(
                "DELETE FROM edge WHERE from_node = ? OR to_node = ?",
                (node_id, node_id),
            )

            # Remove ideas
            self._con.execute("DELETE FROM node_idea WHERE node_id = ?", (node_id,))

            # Remove source row
            self._con.execute("DELETE FROM source WHERE node_id = ?", (node_id,))

            # Remove node row
            self._con.execute("DELETE FROM node WHERE id = ?", (node_id,))
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

        return removed

    # ── Stats ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return high-level vault statistics."""
        try:
            total = self._con.execute("SELECT COUNT(*) FROM node").fetchone()[0]

            by_kind = dict(self._con.execute(
                "SELECT kind, COUNT(*) FROM node GROUP BY kind ORDER BY kind"
            ).fetchall())

            # URL nodes have tier NULL — group by COALESCE(tier, kind) so each
            # kind appears under its own key; legacy rows group under their
            # own kind key too.
            by_tier = dict(self._con.execute(
                "SELECT COALESCE(tier, kind), COUNT(*) FROM node "
                "GROUP BY COALESCE(tier, kind) ORDER BY COALESCE(tier, kind)"
            ).fetchall())

            by_trust = dict(self._con.execute(
                "SELECT trust_state, COUNT(*) FROM node GROUP BY trust_state"
            ).fetchall())

            by_conf = dict(self._con.execute(
                "SELECT COALESCE(confidence, 'unset'), COUNT(*) FROM node GROUP BY confidence"
            ).fetchall())

            # Roots are URL nodes; coverage measures how many content-bearing
            # L0 nodes (legacy raw_source or extracted) have at least one
            # derivation resting on them. Count over e.to_node — the L0 target
            # — never e.from_node: counting from_node would measure the
            # extracted->url edge that every ingested pair has by construction
            # (regression introduced by #98, made the metric read 100% always).
            roots = self._con.execute(
                "SELECT COUNT(*) FROM node WHERE kind = 'url'"
            ).fetchone()[0]
            l0_total = self._con.execute(
                "SELECT COUNT(*) FROM node WHERE kind IN ('raw_source', 'extracted')"
            ).fetchone()[0]
            l0_derived = self._con.execute(
                """
                SELECT COUNT(DISTINCT e.to_node) FROM edge e
                JOIN node n ON n.id = e.to_node
                WHERE e.type = 'provenance' AND e.relation = 'derived_from'
                  AND n.kind IN ('raw_source', 'extracted')
                """
            ).fetchone()[0]
            coverage = round(l0_derived / l0_total * 100, 1) if l0_total > 0 else 0.0

            pending_reviews = self._con.execute(
                "SELECT COUNT(*) FROM event_queue WHERE status = 'pending'"
            ).fetchone()[0]



            return {
                "total_nodes": total,
                "roots": roots,
                "by_kind": by_kind,
                "by_tier": by_tier,
                "by_trust_state": by_trust,
                "by_confidence": by_conf,
                "derivation_coverage_pct": coverage,
                "pending_reviews": pending_reviews,
            }
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

    # ── Retry ──────────────────────────────────────────────────────

    def reset_source_failed(self, node_id: str) -> bool:
        """Set ``source.failed = 0`` and update ``content_path``.

        Returns True if the node existed and was a failed source, False otherwise.
        """
        try:
            row = self._con.execute(
                "SELECT failed FROM source WHERE node_id = ?", (node_id,)
            ).fetchone()
            if row is None or not row["failed"]:
                return False
            self._con.execute(
                "UPDATE source SET failed = 0 WHERE node_id = ?", (node_id,)
            )
            return True
        except sqlite3.Error as e:
            raise StoreError(str(e)) from e

