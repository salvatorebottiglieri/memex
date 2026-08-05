"""Core invariants — L0 registration (url + extracted pair, dedup, ledger).

Contracts of the url+extracted model (ADR-0013, ticket #95):

    R1  register creates exactly one url node + one extracted node + one source row
    R2  url node: kind=url, depth=0, confidence NULL, no content_path
    R3  extracted node: kind=extracted, depth=1, content_path set, provenance
        edge extracted->url, confidence from the fetcher map
    R4  source row binds to the url node (canonical_key UNIQUE); the extracted
        node has no source row of its own
    R5  re-registering the same canonical URL is idempotent: status
        already_exists, same node ids, no new rows
    R6  canonical_key is stable: tracking params, fragments, case, default
        ports, youtube shortlinks
    R7  register without a source_url fails cleanly (missing_source_url)

Every invariant is observable through the CLI (the canonical interface,
ADR-0010) plus direct Store reads.
"""
from __future__ import annotations

import json
import sqlite3

from memex.canonical_key import canonical_key
from memex.store import Store
from tests.conftest import _counts, _run_memex, register_node


def _query(store, sql: str, params: tuple = ()) -> list:
    con = sqlite3.connect(str(store["db"]))
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


class TestRegistrationPair:
    def test_register_creates_url_extracted_pair(self, store):
        result = register_node(store, store["vault"], "a.md", "https://example.com/a")
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["status"] == "registered"

        urls, exts, srcs = _counts(store["db"])
        assert (urls, exts, srcs) == (1, 1, 1)

        # R2 — url node shape
        (url_id, depth, conf, content_path, kind) = _query(
            store, "SELECT id, depth, confidence, content_path, kind FROM node WHERE kind='url'"
        )[0]
        assert depth == 0
        assert conf is None
        assert content_path is None
        assert url_id == data["url_node_id"]

        # R3 — extracted node shape
        (ext_id, ext_depth, ext_kind) = _query(
            store, "SELECT id, depth, kind FROM node WHERE kind='extracted'"
        )[0]
        assert ext_depth == 1
        assert ext_id == data["extracted_node_id"]

        # R3 — provenance edge extracted -> url
        edges = _query(
            store,
            "SELECT from_node, to_node, type, relation FROM edge WHERE relation='derived_from'",
        )
        assert edges == [(ext_id, url_id, "provenance", "derived_from")]

    def test_extracted_node_has_content_path_and_fetcher_confidence(self, store):
        register_node(store, store["vault"], "a.md", "https://example.com/a")
        (content_path, confidence, fetcher) = _query(
            store,
            "SELECT n.content_path, n.confidence, n.fetcher_type FROM node n "
            "WHERE n.kind='extracted'",
        )[0]
        assert content_path is not None
        # R3 — confidence/fetcher are set at extract time, not register time.
        assert confidence is None
        assert fetcher is None

        # R3 — once extracted, confidence follows the fetcher map.
        con = sqlite3.connect(str(store["db"]))
        try:
            st = Store(con)
            (node_id,) = _query(store, "SELECT id FROM node WHERE kind='extracted'")[0]
            st.update_extracted_fetcher(node_id, "http", content_path)
            assert st.compute_node_confidence(node_id) == "medium"
        finally:
            con.close()

    def test_source_row_binds_to_url_node_only(self, store):
        register_node(store, store["vault"], "a.md", "https://example.com/a")
        rows = _query(
            store,
            "SELECT s.node_id, s.canonical_key, s.source_url FROM source s",
        )
        assert len(rows) == 1
        (src_node_id, ckey, src_url) = rows[0]
        (url_id,) = _query(store, "SELECT id FROM node WHERE kind='url'")[0]
        assert src_node_id == url_id
        assert src_url == "https://example.com/a"
        assert ckey == "https://example.com/a"


class TestRegistrationIdempotence:
    def test_register_same_url_twice_is_idempotent(self, store):
        first = register_node(store, store["vault"], "a.md", "https://example.com/a")
        assert json.loads(first.stdout)["status"] == "registered"
        second = register_node(store, store["vault"], "b.md", "https://example.com/a")
        assert second.returncode == 0, second.stderr

        data = json.loads(second.stdout)
        assert data["status"] == "already_exists"
        assert data["extracted_node_id"] == json.loads(first.stdout)["extracted_node_id"]
        assert data["url_node_id"] == json.loads(first.stdout)["url_node_id"]

        # R5 — no new rows, canonical key unchanged
        assert _counts(store["db"]) == (1, 1, 1)
        (ckey,) = _query(store, "SELECT canonical_key FROM source")[0]
        assert ckey == "https://example.com/a"

    def test_register_tracking_variant_is_same_url(self, store):
        register_node(store, store["vault"], "a.md", "https://example.com/a")
        result = register_node(
            store, store["vault"], "b.md", "https://example.com/a?utm_source=newsletter&utm_medium=email"
        )
        data = json.loads(result.stdout)
        assert data["status"] == "already_exists"
        assert _counts(store["db"]) == (1, 1, 1)

    def test_register_without_source_url_fails(self, store):
        path = store["vault"] / "no-url.md"
        path.write_text("# No source url here\n\nBody text that is long enough to register.\n", encoding="utf-8")
        result = _run_memex(
            ["register", "--db", str(store["db"]), "--vault", str(store["vault"]), str(path)]
        )
        assert result.returncode != 0
        assert "missing_source_url" in result.stderr
        assert _counts(store["db"]) == (0, 0, 0)


class TestCanonicalKeyStability:
    def test_fragment_stripped(self):
        assert canonical_key("https://example.com/page#section") == "https://example.com/page"

    def test_tracking_params_stripped(self):
        assert (
            canonical_key("https://example.com/page?utm_source=x&utm_medium=y&id=1")
            == "https://example.com/page?id=1"
        )

    def test_host_lowercased_default_port_stripped(self):
        assert canonical_key("HTTPS://EXAMPLE.COM:443/Page") == "https://example.com/Page"

    def test_trailing_slash_stripped(self):
        assert canonical_key("https://example.com/page/") == "https://example.com/page"

    def test_youtube_shortlink_normalized(self):
        assert canonical_key("https://youtu.be/dQw4w9WgXcQ") == "youtube://dQw4w9WgXcQ"

    def test_youtube_watch_with_tracking_normalized(self):
        assert (
            canonical_key("https://www.youtube.com/watch?v=dQw4w9WgXcQ&feature=share")
            == "youtube://dQw4w9WgXcQ"
        )
