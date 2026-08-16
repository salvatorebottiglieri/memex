"""Tests for `memex ingest --from-inbox` — inbox flush through the shared extract path.

Covers: ledger dedup (System Invariant 2), failed-fetch retry (System
Invariant 4), non-ingestable handling, append-only inbox semantics, and the
--from-inbox requirement. All fetches hit a local stdlib HTTP server in a
thread — no real network.
"""
from __future__ import annotations

import http.server
import json
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from tests.conftest import _counts, _q, _run_memex

_WEB_BODY = (
    "<html><head><title>Memex Test Article</title></head><body>"
    "<h1>Memex Test Article</h1>"
    "<p>This is a longer article body that exceeds the minimum character "
    "threshold of one hundred characters so that the deterministic checks "
    "pass and the extracted node becomes auto-verified.</p>"
    "</body></html>"
)


class _RouteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, body, ctype = self.server.routes.get(self.path, (404, b"not found", "text/plain"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


class _LocalServer:
    """In-process HTTP server with mutable canned routes."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, str]] = {}
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RouteHandler)
        self.httpd.routes = self.routes
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def route(self, path: str, body: str, status: int = 200) -> None:
        self.routes[path] = (status, body.encode("utf-8"), "text/html")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def local_server():
    server = _LocalServer()
    yield server
    server.close()


def _add_inbox_item(store, url: str, note: str | None = None) -> None:
    """Insert an inbox row directly (simulates a capture that hasn't ingested yet)."""
    con = sqlite3.connect(str(store["db"]))
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT INTO inbox (source_name, url, timestamp, note, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("telegram:saved_messages", url, "2024-06-01T09:00:00", note, now),
    )
    con.commit()
    con.close()


def _from_inbox(store):
    """Run `memex ingest --from-inbox` and return the CompletedProcess."""
    result = _run_memex(
        ["ingest", "--from-inbox", "--db", str(store["db"]), "--vault", str(store["vault"])],
    )
    assert result.returncode == 0, result.stderr
    return result


class TestIngestRequiresFlag:
    def test_ingest_without_from_inbox_is_usage_error(self, store):
        result = _run_memex(
            ["ingest", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        assert result.returncode == 2
        assert "--from-inbox" in result.stderr


class TestFromInbox:
    def test_ingests_urls(self, store, local_server):
        local_server.route("/article", _WEB_BODY)
        _add_inbox_item(store, local_server.base_url + "/article")
        data = json.loads(_from_inbox(store).stdout)
        assert len(data) == 1
        assert data[0]["status"] == "extracted"
        assert _counts(store["db"]) == (1, 1, 1)

    def test_returns_json_array(self, store, local_server):
        local_server.route("/a", _WEB_BODY)
        local_server.route("/b", _WEB_BODY)
        _add_inbox_item(store, local_server.base_url + "/a")
        _add_inbox_item(store, local_server.base_url + "/b")
        data = json.loads(_from_inbox(store).stdout)
        assert isinstance(data, list)
        assert len(data) == 2
        assert _counts(store["db"]) == (2, 2, 2)

    def test_empty_inbox_returns_empty_array(self, store):
        assert json.loads(_from_inbox(store).stdout) == []

    def test_progress_lines_on_stderr(self, store, local_server):
        local_server.route("/article", _WEB_BODY)
        _add_inbox_item(store, local_server.base_url + "/article")
        result = _from_inbox(store)
        assert "[1/1] extracted" in result.stderr
        assert local_server.base_url + "/article" in result.stderr

    def test_does_not_delete_inbox_rows(self, store, local_server):
        """Inbox is append-only — rows survive ingest (failed retries need them)."""
        local_server.route("/article", _WEB_BODY)
        _add_inbox_item(store, local_server.base_url + "/article")
        _from_inbox(store)
        _from_inbox(store)
        assert len(_q(store, "SELECT id FROM inbox")) == 1

    def test_preserves_note_in_inbox(self, store, local_server):
        local_server.route("/article", _WEB_BODY)
        _add_inbox_item(store, local_server.base_url + "/article", note="interesting read")
        _from_inbox(store)
        rows = _q(store, "SELECT note FROM inbox")
        assert rows[0][0] == "interesting read"


class TestLedgerDedup:
    """System Invariant 2: the same canonical key never yields a second url/extracted pair."""

    def test_rerun_returns_already_exists_no_duplicate_nodes(self, store, local_server):
        local_server.route("/article", _WEB_BODY)
        _add_inbox_item(store, local_server.base_url + "/article")

        first = json.loads(_from_inbox(store).stdout)
        assert first[0]["status"] == "extracted"
        assert _counts(store["db"]) == (1, 1, 1)

        second = json.loads(_from_inbox(store).stdout)
        assert second[0]["status"] == "already_exists"
        assert _counts(store["db"]) == (1, 1, 1)  # no duplicate node/file

        assert len(list((store["vault"] / "extracted").glob("*.md"))) == 1

    def test_dedup_by_canonical_key_within_one_run(self, store, local_server):
        local_server.route("/article", _WEB_BODY)
        _add_inbox_item(store, local_server.base_url + "/article")
        _add_inbox_item(store, local_server.base_url + "/article?utm_source=x")
        data = json.loads(_from_inbox(store).stdout)
        assert [d["status"] for d in data] == ["extracted", "already_exists"]
        assert _counts(store["db"]) == (1, 1, 1)


class TestEdgeCases:
    def test_not_ingestable_url_advisory_no_nodes(self, store):
        _add_inbox_item(store, "https://x.com/user/status/123")
        data = json.loads(_from_inbox(store).stdout)
        assert len(data) == 1
        assert data[0]["status"] == "not_ingestable"
        assert _counts(store["db"]) == (0, 0, 0)

    def test_no_content_url(self, store, local_server):
        local_server.route("/js-only", "<html><body><div id='app'></div></body></html>")
        _add_inbox_item(store, local_server.base_url + "/js-only")
        data = json.loads(_from_inbox(store).stdout)
        assert data[0]["status"] == "no_content"
        assert _counts(store["db"]) == (1, 0, 1)


class TestFailedFetchRetry:
    """System Invariant 4: a failed fetch never consumes the inbox row."""

    def test_failed_fetch_leaves_row_pending_and_second_run_retries(self, store, local_server):
        local_server.route("/flaky", _WEB_BODY, status=500)
        _add_inbox_item(store, local_server.base_url + "/flaky")

        first = json.loads(_from_inbox(store).stdout)
        assert first[0]["status"] == "fetch_failed"
        # The row is still pending — nothing was consumed.
        assert len(_q(store, "SELECT id FROM inbox")) == 1
        # A failed URL node was recorded (failed=1), no extracted child yet.
        con = sqlite3.connect(str(store["db"]))
        failed = con.execute("SELECT failed FROM source").fetchone()[0]
        con.close()
        assert failed == 1
        assert _counts(store["db"]) == (1, 0, 1)

        # Second run retries the same URL and succeeds.
        local_server.route("/flaky", _WEB_BODY, status=200)
        second = json.loads(_from_inbox(store).stdout)
        assert second[0]["status"] == "extracted"
        assert _counts(store["db"]) == (1, 1, 1)
