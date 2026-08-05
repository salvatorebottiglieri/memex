"""Tests for ticket #96 — `memex extract <url>`.

Covers: web-page extraction (url+extracted pair, fetcher_type/confidence/
trust), PDF routing, canonical-key dedup, in-place re-extract, non-ingestable
advisory, and fetch failures. All fetches hit a local stdlib HTTP server in a
thread — no real network.
"""
from __future__ import annotations

import http.server
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memex.fetchers import (
    FetchError,
    HttpFetcher,
    PDFFetcher,
    WikipediaFetcher,
    fetch,
    get_fetcher,
)
from memex.resolve.rules import Resolution

from tests.conftest import _counts, _run_memex


# ── helpers ──────────────────────────────────────────────────────────

_WEB_BODY = (
    "<html><head><title>Memex Test Article</title></head><body>"
    "<h1>Memex Test Article</h1>"
    "<p>This is a longer article body that exceeds the minimum character "
    "threshold of one hundred characters so that the deterministic checks "
    "pass and the extracted node becomes auto-verified.</p>"
    "<p>> Synthesis: this page demonstrates the checks-to-trust pattern.</p>"
    "</body></html>"
)


def _make_pdf(text: str) -> bytes:
    """Build a deterministic minimal single-page PDF containing ``text``."""
    esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    stream = f"BT /F1 24 Tf 72 720 Td ({esc}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode()
        out += obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


class _RouteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, body, ctype, content_length = self.server.routes.get(
            self.path, (404, b"not found", "text/plain", "auto")
        )
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if content_length == "auto":
            self.send_header("Content-Length", str(len(body)))
        elif content_length is not None:
            self.send_header("Content-Length", str(content_length))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


class _LocalServer:
    """In-process HTTP server with mutable canned routes."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, str, object]] = {}
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RouteHandler)
        self.httpd.routes = self.routes
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self._closed = False

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def route(self, path, body, content_type="text/html", status=200, content_length="auto"):
        """Register a canned route.

        ``content_length``: "auto" (default) sends the real body length,
        ``None`` omits the header (EOF-delimited body), any int overrides it.
        """
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[path] = (status, body, content_type, content_length)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def local_server():
    server = _LocalServer()
    yield server
    server.close()


@pytest.fixture
def run_extract(store):
    """Run `memex extract <url> [args]` against the fixture db/vault."""

    def _run(url: str, *extra: str) -> dict:
        proc = _run_memex(
            ["extract", *extra, "--db", str(store["db"]), "--vault", str(store["vault"]), url]
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    return _run


# ── AC1: web page → url+extracted pair ───────────────────────────────

def test_extract_web_page_creates_url_and_extracted_pair(store, local_server, run_extract):
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"
    data = run_extract(url)

    assert data["status"] == "extracted"
    assert data["fetcher_type"] == "http"
    assert data["confidence"] == "medium"
    assert data["trust_state"] == "auto-verified"
    assert data["title"] == "Memex Test Article"
    url_id, ext_id = data["url_node_id"], data["extracted_node_id"]
    assert url_id and ext_id and url_id != ext_id

    # .md content on disk under vault/extracted/
    content_path = Path(data["content_path"])
    assert content_path.parent == store["vault"] / "extracted"
    assert content_path.exists()
    text = content_path.read_text(encoding="utf-8")
    assert "Memex Test Article" in text
    assert "> Synthesis:" in text

    # DB rows: url node + extracted node + provenance edge + source
    con = sqlite3.connect(store["db"])
    con.row_factory = sqlite3.Row
    try:
        url_row = con.execute("SELECT * FROM node WHERE id = ?", (url_id,)).fetchone()
        ext_row = con.execute("SELECT * FROM node WHERE id = ?", (ext_id,)).fetchone()
        edge = con.execute(
            "SELECT * FROM edge WHERE from_node = ? AND type = 'provenance' "
            "AND relation = 'derived_from'",
            (ext_id,),
        ).fetchone()
        src = con.execute("SELECT * FROM source WHERE node_id = ?", (url_id,)).fetchone()
    finally:
        con.close()
    assert url_row["kind"] == "url"
    assert url_row["tier"] is None
    assert url_row["trust_state"] is None
    assert url_row["depth"] == 0
    assert ext_row["kind"] == "extracted"
    assert ext_row["tier"] == "extracted"
    assert ext_row["depth"] == 1
    assert ext_row["fetcher_type"] == "http"
    assert ext_row["confidence"] == "medium"
    assert ext_row["trust_state"] == "auto-verified"
    assert edge is not None and edge["to_node"] == url_id
    assert src["failed"] == 0
    assert src["title"] == "Memex Test Article"
    assert src["fetched_at"] is not None
    assert src["canonical_key"] == "http://127.0.0.1:%d/article" % local_server.httpd.server_address[1]


# ── AC1b: JS-only page → no_content (ADR-0013 absence) ───────────────

def test_extract_js_only_page_stores_nothing(store, local_server, run_extract):
    """A page with no extractable text is an expected absence (ADR-0013):
    the URL node + source are recorded, no extracted node and no .md file."""
    local_server.route(
        "/slides",
        "<html><head><script>window['ppConfig']={productName:'x'}</script></head>"
        "<body></body></html>",
    )
    url = local_server.base_url + "/slides"
    data = run_extract(url)

    assert data["status"] == "no_content"
    assert "extracted_node_id" not in data

    # url node + source recorded; no extracted node, no content file
    assert _counts(store["db"]) == (1, 0, 1)
    assert not list(Path(store["vault"]).glob("extracted/*.md"))


# ── AC2: PDF URL → PDF fetcher, confidence=high ──────────────────────

def test_extract_pdf_routes_to_pdf_fetcher(store, local_server, run_extract):
    pdf_text = "> Synthesis: PDF test summary. " + "Extracted page text for memex. " * 6
    local_server.route("/paper.pdf", _make_pdf(pdf_text), content_type="application/pdf")
    url = local_server.base_url + "/paper.pdf"
    data = run_extract(url)

    assert data["status"] == "extracted"
    assert data["fetcher_type"] == "pdf"
    assert data["confidence"] == "high"
    assert data["trust_state"] == "auto-verified"
    content = Path(data["content_path"]).read_text(encoding="utf-8")
    assert "Extracted page text for memex." in content
    assert "> Synthesis:" in content
    assert _counts(store["db"]) == (1, 1, 1)


# ── AC3a: dedup by canonical key → already_exists ────────────────────

def test_reextract_same_url_returns_already_exists(store, local_server, run_extract):
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"
    first = run_extract(url)
    second = run_extract(url)

    assert second["status"] == "already_exists"
    assert second["url_node_id"] == first["url_node_id"]
    assert second["extracted_node_id"] == first["extracted_node_id"]
    # nothing duplicated
    assert _counts(store["db"]) == (1, 1, 1)
    assert store["vault"].joinpath("extracted").is_dir()
    assert len(list((store["vault"] / "extracted").glob("*.md"))) == 1


# ── AC3b: --force re-extract regenerates content in place ────────────

def test_force_reextract_regenerates_content_in_place(store, local_server, run_extract):
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"
    first = run_extract(url)
    first_path = Path(first["content_path"])

    # Server content changes; --force must re-fetch and overwrite in place.
    local_server.route("/article", _WEB_BODY.replace("Memex Test Article", "Updated Article"))
    data = run_extract(url, "--force")

    assert data["status"] == "re_extracted"
    assert data["url_node_id"] == first["url_node_id"]
    assert data["extracted_node_id"] == first["extracted_node_id"]
    assert data["content_path"] == first["content_path"]
    assert Path(data["content_path"]) == first_path
    content = first_path.read_text(encoding="utf-8")
    assert "Updated Article" in content
    assert "Memex Test Article" not in content
    # Same two nodes, still one source row, title refreshed
    assert _counts(store["db"]) == (1, 1, 1)
    con = sqlite3.connect(store["db"])
    try:
        title, failed = con.execute(
            "SELECT title, failed FROM source WHERE node_id = ?", (first["url_node_id"],)
        ).fetchone()
    finally:
        con.close()
    assert title == "Updated Article"
    assert failed == 0


# ── failed-state retry reuses the URL node ───────────────────────────

def test_retry_after_fetch_failure_reuses_url_node(store, local_server, run_extract):
    local_server.route("/article", _WEB_BODY, status=500)
    url = local_server.base_url + "/article"
    first = run_extract(url)
    assert first["status"] == "fetch_failed"

    # Server heals → retry succeeds, same URL node, source flipped to ok.
    local_server.route("/article", _WEB_BODY, status=200)
    second = run_extract(url)
    assert second["status"] == "extracted"
    assert second["url_node_id"] == first["url_node_id"]
    assert _counts(store["db"]) == (1, 1, 1)
    con = sqlite3.connect(store["db"])
    try:
        failed, title = con.execute(
            "SELECT failed, title FROM source WHERE node_id = ?", (first["url_node_id"],)
        ).fetchone()
    finally:
        con.close()
    assert failed == 0
    assert title == "Memex Test Article"


# ── AC4: non-ingestable → advisory, no nodes ─────────────────────────

def test_non_ingestable_url_returns_advisory_and_creates_no_nodes(store, local_server, run_extract):
    data = run_extract("https://x.com/user/status/123")
    assert data["status"] == "not_ingestable"
    assert data["type"] == "unknown"
    assert data["ingestable"] is False
    assert "direct_url" in data
    assert data["direct_url"] is None
    assert data["note"]
    assert _counts(store["db"]) == (0, 0, 0)
    assert not (store["vault"] / "extracted").exists()


def test_non_ingestable_advisory_does_not_require_db(tmp_path):
    db = tmp_path / "never-created.db"
    proc = _run_memex(
        ["extract", "--db", str(db), "--vault", str(tmp_path / "vault"), "https://x.com/user/status/123"]
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["status"] == "not_ingestable"
    assert not db.exists()


# ── AC5: fetch failure → failed URL node, no extracted ───────────────

def test_fetch_failure_404_creates_failed_url_node(store, local_server, run_extract):
    local_server.route("/broken", "gone", status=404)
    url = local_server.base_url + "/broken"
    data = run_extract(url)

    assert data["status"] == "fetch_failed"
    assert "url_node_id" in data
    assert "404" in data["error"]
    assert _counts(store["db"]) == (1, 0, 1)
    con = sqlite3.connect(store["db"])
    try:
        kind = con.execute(
            "SELECT kind FROM node WHERE id = ?", (data["url_node_id"],)
        ).fetchone()[0]
        failed = con.execute(
            "SELECT failed FROM source WHERE node_id = ?", (data["url_node_id"],)
        ).fetchone()[0]
    finally:
        con.close()
    assert kind == "url"
    assert failed == 1


def test_fetch_failure_connection_refused_creates_failed_url_node(store, local_server, run_extract):
    url = local_server.base_url + "/down"
    local_server.close()  # port now closed → connection refused
    data = run_extract(url)

    assert data["status"] == "fetch_failed"
    assert "url_node_id" in data
    assert data["error"]
    assert _counts(store["db"]) == (1, 0, 1)


# ── trust: draft → checks → auto-verified (or draft when checks fail) ─

def test_short_content_stays_draft_with_check_failures(store, local_server, run_extract):
    local_server.route("/short", "<html><body><p>tiny</p></body></html>")
    url = local_server.base_url + "/short"
    data = run_extract(url)

    assert data["status"] == "extracted"
    assert data["trust_state"] == "draft"
    con = sqlite3.connect(store["db"])
    try:
        row = con.execute(
            "SELECT trust_state, check_failures FROM node WHERE id = ?",
            (data["extracted_node_id"],),
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "draft"
    assert row[1] is not None  # check_failures persisted


# ── fetcher router unit tests ────────────────────────────────────────

class TestFetcherRouter:
    def test_arxiv_resolution_routes_to_pdf(self):
        res = Resolution(
            url="https://arxiv.org/abs/2304.12345",
            type="arxiv", ingestable=True,
            direct_url="https://arxiv.org/pdf/2304.12345",
        )
        assert get_fetcher(res.url, res) is PDFFetcher

    def test_pdf_direct_url_routes_to_pdf(self):
        res = Resolution(
            url="https://example.com/paper", type="web", ingestable=True,
            direct_url="https://cdn.example.com/paper.pdf",
        )
        assert get_fetcher(res.url, res) is PDFFetcher

    def test_url_ending_in_pdf_routes_to_pdf(self):
        res = Resolution(url="https://example.com/paper.pdf", type="web", ingestable=True)
        assert get_fetcher(res.url, res) is PDFFetcher

    def test_web_resolution_routes_to_http(self):
        res = Resolution(url="https://example.com/article", type="web", ingestable=True)
        assert get_fetcher(res.url, res) is HttpFetcher

    def test_unknown_type_defaults_to_http(self):
        res = Resolution(url="https://example.com/feed", type="feed", ingestable=True)
        assert get_fetcher(res.url, res) is HttpFetcher

    def test_wikipedia_resolution_routes_to_wikipedia_fetcher(self):
        res = Resolution(
            url="https://en.wikipedia.org/wiki/Albert_Einstein",
            type="wikipedia", ingestable=True,
            direct_url="https://en.wikipedia.org/api/rest_v1/page/summary/Albert_Einstein",
        )
        assert get_fetcher(res.url, res) is WikipediaFetcher

    def test_wikipedia_pdf_title_stays_on_wikipedia_fetcher(self):
        """A wiki title ending in .pdf (e.g. File:Example.pdf) must not be
        misrouted to the PDF fetcher: the wikipedia direct_url is the REST
        summary endpoint, whose path happens to end in .pdf — the .pdf
        override only applies to non-wikipedia resolutions."""
        res = Resolution(
            url="https://en.wikipedia.org/wiki/File:Example.pdf",
            type="wikipedia", ingestable=True,
            direct_url="https://en.wikipedia.org/api/rest_v1/page/summary/File:Example.pdf",
        )
        assert get_fetcher(res.url, res) is WikipediaFetcher

    def test_fetch_uses_resolution_direct_url(self, local_server):
        res = Resolution(
            url="https://example.com/original", type="web", ingestable=True,
            direct_url=local_server.base_url + "/article",
        )
        local_server.route("/article", _WEB_BODY)
        result = fetch(res.url, res)
        assert result.title == "Memex Test Article"
        assert "Memex Test Article" in result.content


class TestHttpFetcher:
    def test_extracts_title_and_stripped_text(self, local_server):
        local_server.route("/page", _WEB_BODY)
        result = HttpFetcher().fetch(local_server.base_url + "/page")
        assert result.title == "Memex Test Article"
        assert "Memex Test Article" in result.content
        assert "<html>" not in result.content
        assert "<title>" not in result.content

    def test_non_200_raises_fetch_error(self, local_server):
        local_server.route("/missing", "gone", status=404)
        with pytest.raises(FetchError):
            HttpFetcher().fetch(local_server.base_url + "/missing")

    def test_network_error_raises_fetch_error(self, local_server):
        url = local_server.base_url + "/down"
        local_server.close()
        with pytest.raises(FetchError):
            HttpFetcher().fetch(url)


class TestPDFFetcher:
    def test_extracts_page_text(self, local_server):
        local_server.route(
            "/doc.pdf", _make_pdf("PDF page one text for memex."),
            content_type="application/pdf",
        )
        result = PDFFetcher().fetch(local_server.base_url + "/doc.pdf")
        assert "PDF page one text for memex." in result.content


class TestWikipediaFetcher:
    _SUMMARY = {
        "type": "summary",
        "title": "Albert Einstein",
        "displaytitle": "Albert Einstein",
        "description": "German-born theoretical physicist",
        "extract": (
            "Albert Einstein was a German-born theoretical physicist who "
            "developed the theory of relativity, one of the two pillars of "
            "modern physics alongside quantum mechanics."
        ),
    }

    def test_summary_json_rendered_as_readable_prose(self, local_server):
        """The REST summary JSON must be converted to prose, never HTML-stripped
        into raw machine JSON in the extracted .md."""
        local_server.route(
            "/summary", json.dumps(self._SUMMARY), content_type="application/json"
        )
        result = WikipediaFetcher().fetch(local_server.base_url + "/summary")

        assert result.title == "Albert Einstein"
        assert "# Albert Einstein" in result.content
        assert "German-born theoretical physicist" in result.content
        assert "developed the theory of relativity" in result.content
        assert '{"type": "summary"' not in result.content

    def test_invalid_json_raises_fetch_error(self, local_server):
        local_server.route("/garbage", "not json at all", content_type="application/json")
        with pytest.raises(FetchError):
            WikipediaFetcher().fetch(local_server.base_url + "/garbage")

    def test_non_object_json_raises_fetch_error(self, local_server):
        """Valid JSON that is not an object (list/str/number/null) must raise
        a clean FetchError, not an uncaught AttributeError from .get."""
        local_server.route("/list", b"[]", content_type="application/json")
        with pytest.raises(FetchError, match="not a JSON object"):
            WikipediaFetcher().fetch(local_server.base_url + "/list")

    def test_error_shape_without_prose_raises_fetch_error(self, local_server):
        """HyperSwitch error responses carry a title but no extract/description
        — refuse them instead of extracting a one-line error page."""
        local_server.route(
            "/missing",
            json.dumps({
                "type": "https://mediawiki.org/wiki/HyperSwitch/errors/not_found",
                "title": "Not found.",
            }),
            content_type="application/json",
        )
        with pytest.raises(FetchError, match="no readable prose"):
            WikipediaFetcher().fetch(local_server.base_url + "/missing")


# ── finding 1: ledger hit on a non-url node → already_registered ──────

def test_extract_url_registered_as_legacy_raw_source_returns_already_registered(store, local_server, run_extract):
    """A legacy raw_source node owns the canonical key for S (pre-#97 DB), so
    `memex extract S` must report already_registered instead of rewriting that
    node's source row and crashing with an uncaught ValueError in create_node."""
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"

    # Seed a legacy raw_source node + source row directly (expand-phase DB).
    from memex.canonical_key import canonical_key
    from memex.store import Store

    vault = store["vault"]
    legacy_id = str(uuid.uuid4())
    con = sqlite3.connect(store["db"])
    st = Store(con)
    st.create_node(node_id=legacy_id, kind="raw_source", depth=0, content_path="",
                   created_at=datetime.now(timezone.utc).isoformat())
    st.attach_source(node_id=legacy_id, canonical_key=canonical_key(url), source_url=url,
                     title="L0 Article", fetched_at=None)
    con.commit()
    con.close()

    data = run_extract(url)

    assert data["status"] == "already_registered"
    assert data["node_id"] == legacy_id
    # No URL/extracted node and no new source row were created.
    assert _counts(store["db"]) == (0, 0, 1)
    assert not (vault / "extracted").exists()

    # The raw_source's source row is untouched — no title/fetched_at rewrite.
    con = sqlite3.connect(store["db"])
    try:
        row = con.execute(
            "SELECT title, fetched_at, failed FROM source WHERE node_id = ?", (legacy_id,)
        ).fetchone()
    finally:
        con.close()
    assert row[0] == "L0 Article"
    assert row[1] is None
    assert row[2] == 0


# ── finding 2: file writes transactional with the DB ─────────────────

def test_fresh_extract_failure_unlinks_orphan_md(store, local_server):
    """A failure after writing a fresh .md must unlink it: the DB transaction
    rolls back, so leaving the file would orphan it."""
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"

    # Sabotage the first node insert so the write -> DB sequence fails.
    con = sqlite3.connect(store["db"])
    con.execute(
        "CREATE TRIGGER fail_node_insert BEFORE INSERT ON node "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    con.commit()
    con.close()

    proc = _run_memex(
        ["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), url]
    )
    assert proc.returncode != 0
    # No orphan .md and no committed rows (transaction rolled back).
    assert not (store["vault"] / "extracted").exists() or not list(
        (store["vault"] / "extracted").glob("*.md")
    )
    assert _counts(store["db"]) == (0, 0, 0)


def test_force_failure_leaves_existing_file_untouched(store, local_server):
    """--force must defer the in-place overwrite until the DB writes have
    succeeded: if a DB write fails, the old file and old DB state survive."""
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"
    first = json.loads(
        _run_memex(["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), url]).stdout
    )
    md_path = Path(first["content_path"])
    assert "Memex Test Article" in md_path.read_text(encoding="utf-8")

    # Server content changes; a DB failure must stop --force before the file.
    local_server.route("/article", _WEB_BODY.replace("Memex Test Article", "Updated Article"))
    con = sqlite3.connect(store["db"])
    con.execute(
        "CREATE TRIGGER fail_source_update BEFORE UPDATE ON source "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    con.commit()
    con.close()

    proc = _run_memex(
        ["extract", "--force", "--db", str(store["db"]), "--vault", str(store["vault"]), url]
    )
    assert proc.returncode != 0
    # File NOT overwritten (deferred past the DB writes), DB title unchanged.
    content = md_path.read_text(encoding="utf-8")
    assert "Memex Test Article" in content
    assert "Updated Article" not in content
    con = sqlite3.connect(store["db"])
    try:
        title = con.execute(
            "SELECT title FROM source WHERE node_id = ?", (first["url_node_id"],)
        ).fetchone()[0]
    finally:
        con.close()
    assert title == "Memex Test Article"


def test_force_failure_after_trust_update_keeps_old_file_and_state(store, local_server):
    """The --force overwrite must be the LAST step before commit: a failure
    in the checks/trust step (here: a trigger aborting the node trust_state
    UPDATE, after the source/fetcher writes succeeded) must leave the OLD
    .md content and OLD DB state intact and discard the temp file."""
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"
    first = json.loads(
        _run_memex(["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), url]).stdout
    )
    md_path = Path(first["content_path"])
    assert "Memex Test Article" in md_path.read_text(encoding="utf-8")

    # Server content changes; abort on the node trust_state UPDATE — the
    # final DB write, which previously sat AFTER the file overwrite.
    local_server.route("/article", _WEB_BODY.replace("Memex Test Article", "Updated Article"))
    con = sqlite3.connect(store["db"])
    con.execute(
        "CREATE TRIGGER fail_trust_update BEFORE UPDATE OF trust_state ON node "
        "BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    con.commit()
    con.close()

    proc = _run_memex(
        ["extract", "--force", "--db", str(store["db"]), "--vault", str(store["vault"]), url]
    )
    assert proc.returncode != 0

    # File keeps the OLD content; DB keeps the OLD state; temp file cleaned.
    content = md_path.read_text(encoding="utf-8")
    assert "Memex Test Article" in content
    assert "Updated Article" not in content
    con = sqlite3.connect(store["db"])
    try:
        title = con.execute(
            "SELECT title FROM source WHERE node_id = ?", (first["url_node_id"],)
        ).fetchone()[0]
        trust_state, fetcher_type = con.execute(
            "SELECT trust_state, fetcher_type FROM node WHERE id = ?",
            (first["extracted_node_id"],),
        ).fetchone()
    finally:
        con.close()
    assert title == "Memex Test Article"
    assert (trust_state, fetcher_type) == ("auto-verified", "http")
    assert list((store["vault"] / "extracted").glob("*.tmp")) == []


# ── finding 3: bounded download (no unbounded buffering) ─────────────

class TestDownloadSizeCap:
    def test_rejects_oversized_content_length_header(self, local_server):
        from memex.fetchers.http import download_bytes

        local_server.route("/huge", b"x" * 1024, content_length=10 ** 9)
        with pytest.raises(FetchError, match="too large"):
            download_bytes(local_server.base_url + "/huge")

    def test_aborts_stream_once_past_cap(self, local_server, monkeypatch):
        from memex.fetchers import http as http_module

        monkeypatch.setattr(http_module, "_MAX_DOWNLOAD_BYTES", 2048)
        # No Content-Length header → the streaming cap must abort mid-body.
        local_server.route("/stream", b"y" * 8192, content_length=None)
        with pytest.raises(FetchError, match="too large"):
            http_module.download_bytes(local_server.base_url + "/stream")

    def test_body_under_cap_downloads_fully(self, local_server, monkeypatch):
        from memex.fetchers import http as http_module

        monkeypatch.setattr(http_module, "_MAX_DOWNLOAD_BYTES", 2048)
        local_server.route("/small", b"z" * 1024)
        assert http_module.download_bytes(local_server.base_url + "/small") == b"z" * 1024


# ── finding 4: PDF routing by URL path (query/fragment safe) ─────────

class TestPdfRoutingByPath:
    def test_query_string_and_fragment_still_route_to_pdf(self):
        res = Resolution(
            url="https://example.com/paper.pdf?v=2#section-3",
            type="web", ingestable=True,
        )
        assert get_fetcher(res.url, res) is PDFFetcher

    def test_pdf_in_query_only_does_not_route_to_pdf(self):
        res = Resolution(
            url="https://example.com/view?file=paper.pdf",
            type="web", ingestable=True,
        )
        assert get_fetcher(res.url, res) is HttpFetcher

    def test_pdf_direct_url_with_query_routes_to_pdf(self):
        res = Resolution(
            url="https://example.com/paper", type="web", ingestable=True,
            direct_url="https://cdn.example.com/paper.pdf?download=1",
        )
        assert get_fetcher(res.url, res) is PDFFetcher

    def test_uppercase_path_extension_routes_to_pdf(self):
        res = Resolution(url="https://example.com/paper.PDF", type="web", ingestable=True)
        assert get_fetcher(res.url, res) is PDFFetcher


# ── finding 6: source-row updates go through Store methods ───────────

class TestExtractStoreHelpers:
    def test_mark_source_failed_and_update_source_after_fetch(self, db_store):
        node_id = "n1"
        db_store.create_node(node_id=node_id, kind="url")
        db_store.attach_source(
            node_id=node_id, canonical_key="ck", source_url="https://example.com",
            title="Old", fetched_at="t0",
        )
        db_store.mark_source_failed(node_id, "t1")
        row = db_store.get_node(node_id)
        assert row["failed"] is True
        assert row["fetched_at"] == "t1"
        db_store.update_source_after_fetch(node_id, "New Title", "t2")
        row = db_store.get_node(node_id)
        assert row["failed"] is False
        assert row["fetched_at"] == "t2"
        assert row["title"] == "New Title"

    def test_update_extracted_fetcher_refreshes_confidence(self, db_store):
        url_id = "u1"
        ext_id = "e1"
        db_store.create_node(node_id=url_id, kind="url")
        db_store.create_node(
            node_id=ext_id, kind="extracted", fetcher_type="http",
            content_path="/tmp/e1.md", derived_from=url_id,
        )
        assert db_store.get_node(ext_id)["confidence"] == "medium"
        db_store.update_extracted_fetcher(ext_id, "pdf")
        node = db_store.get_node(ext_id)
        assert node["fetcher_type"] == "pdf"
        assert node["confidence"] == "high"

    def test_update_extracted_fetcher_updates_content_path(self, db_store):
        """A re-extract may move the node's file (fetcher cache artifact vs
        CLI-owned vault/extracted file): the row must track it (ticket #99)."""
        url_id = "u1"
        ext_id = "e1"
        db_store.create_node(node_id=url_id, kind="url")
        db_store.create_node(
            node_id=ext_id, kind="extracted", fetcher_type="youtube",
            content_path="/tmp/e1.md", derived_from=url_id,
        )
        db_store.update_extracted_fetcher(
            ext_id, "youtube", content_path="/vault/.cache/youtube-abc123.md"
        )
        node = db_store.get_node(ext_id)
        assert node["content_path"] == "/vault/.cache/youtube-abc123.md"
        # Without content_path the existing path is preserved.
        db_store.update_extracted_fetcher(ext_id, "http")
        node = db_store.get_node(ext_id)
        assert node["content_path"] == "/vault/.cache/youtube-abc123.md"
        assert node["fetcher_type"] == "http"


# ── finding 7: dedup filters kind='extracted' ────────────────────────

def test_dedup_finds_extracted_child_not_other_derivations(store, local_server, run_extract):
    """A URL node with an earlier-sorted non-extracted derivation child must
    still dedup to its extracted child: the child lookup filters
    kind='extracted' instead of taking the first derived_from edge."""
    from memex.canonical_key import canonical_key

    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"
    ckey = canonical_key(url)
    now = "2026-01-01T00:00:00+00:00"
    url_id, summary_id = "url-1", "summary-1"

    # url node + source row + a non-extracted summary child whose provenance
    # edge sorts BEFORE any extracted child's edge (explicit rowid 1).
    con = sqlite3.connect(store["db"])
    con.execute(
        "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at, confidence) "
        "VALUES (?, 'url', NULL, NULL, 0, NULL, ?, NULL)",
        (url_id, now),
    )
    con.execute(
        "INSERT INTO source (node_id, canonical_key, source_url, title, fetched_at, failed) "
        "VALUES (?, ?, ?, 'L0 title', ?, 0)",
        (url_id, ckey, url, now),
    )
    con.execute(
        "INSERT INTO node (id, kind, tier, trust_state, depth, content_path, created_at, confidence) "
        "VALUES (?, 'summary', 'notes', 'auto-verified', 1, ?, ?, 'medium')",
        (summary_id, str(store["vault"] / "summary.md"), now),
    )
    con.execute(
        "INSERT INTO edge (rowid, id, type, relation, from_node, to_node) "
        "VALUES (1, 'edge-summary', 'provenance', 'derived_from', ?, ?)",
        (summary_id, url_id),
    )
    con.commit()
    con.close()

    # First extract creates the extracted child; the second must dedup to it
    # even though the summary edge sorts first.
    first = run_extract(url)
    assert first["status"] == "extracted"
    second = run_extract(url)
    assert second["status"] == "already_exists"
    assert second["url_node_id"] == url_id
    assert second["extracted_node_id"] == first["extracted_node_id"]
    assert _counts(store["db"]) == (1, 1, 1)

    # --force must also regenerate the extracted node in place, not the
    # summary child.
    forced = run_extract(url, "--force")
    assert forced["status"] == "re_extracted"
    assert forced["extracted_node_id"] == first["extracted_node_id"]
    assert _counts(store["db"]) == (1, 1, 1)


# ── finding 8: --force refreshes fetcher_type and confidence ─────────

def test_force_reextract_refreshes_fetcher_type_and_confidence(store, local_server, run_extract):
    """Stale fetcher metadata from a previous different-fetcher extraction
    must not survive a --force regeneration."""
    local_server.route("/article", _WEB_BODY)
    url = local_server.base_url + "/article"
    first = run_extract(url)
    assert first["fetcher_type"] == "http"
    assert first["confidence"] == "medium"

    # Simulate a stale state as if a previous extraction had used the PDF
    # fetcher (fetcher_type/confidence from a different run).
    con = sqlite3.connect(store["db"])
    con.execute(
        "UPDATE node SET fetcher_type = 'pdf', confidence = 'high' WHERE id = ?",
        (first["extracted_node_id"],),
    )
    con.commit()
    con.close()

    data = run_extract(url, "--force")

    assert data["status"] == "re_extracted"
    assert data["extracted_node_id"] == first["extracted_node_id"]
    assert data["fetcher_type"] == "http"
    assert data["confidence"] == "medium"
    con = sqlite3.connect(store["db"])
    try:
        row = con.execute(
            "SELECT fetcher_type, confidence FROM node WHERE id = ?",
            (first["extracted_node_id"],),
        ).fetchone()
    finally:
        con.close()
    assert (row[0], row[1]) == ("http", "medium")


# ── finding 9: wikipedia extract carries confidence='high' ───────────

def test_wikipedia_extract_carries_high_confidence(store, local_server, monkeypatch):
    """EXTRACTED_CONFIDENCE must map the wikipedia fetcher (parallel to pdf):
    a wikipedia extract persists confidence='high' — never NULL — both at
    creation and on --force refresh, and the CLI JSON reports it."""
    from click.testing import CliRunner

    from memex.cli import cli
    from memex.resolve.rules import Resolution

    local_server.route(
        "/summary",
        json.dumps(TestWikipediaFetcher._SUMMARY),
        content_type="application/json",
    )
    direct_url = local_server.base_url + "/summary"

    def fake_resolve(url: str) -> Resolution:
        return Resolution(url=url, type="wikipedia", ingestable=True, direct_url=direct_url)

    # The extract command imports resolve_url inside the function, so patching
    # the module attribute steers the CLI flow to the local server.
    monkeypatch.setattr("memex.resolve.rules.resolve_url", fake_resolve)

    runner = CliRunner()
    url = "https://en.wikipedia.org/wiki/Albert_Einstein"
    first = runner.invoke(
        cli,
        ["extract", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        catch_exceptions=False,
    )
    assert first.exit_code == 0, first.output
    data = json.loads(first.output)
    assert data["status"] == "extracted"
    assert data["fetcher_type"] == "wikipedia"
    assert data["confidence"] == "high"

    forced = runner.invoke(
        cli,
        ["extract", "--force", "--db", str(store["db"]), "--vault", str(store["vault"]), url],
        catch_exceptions=False,
    )
    assert forced.exit_code == 0, forced.output
    data = json.loads(forced.output)
    assert data["status"] == "re_extracted"
    assert data["fetcher_type"] == "wikipedia"
    assert data["confidence"] == "high"

    # The persisted row matches the CLI claim (--force refresh recomputes it).
    con = sqlite3.connect(str(store["db"]))
    try:
        row = con.execute(
            "SELECT fetcher_type, confidence FROM node WHERE id = ?",
            (data["extracted_node_id"],),
        ).fetchone()
    finally:
        con.close()
    assert (row[0], row[1]) == ("wikipedia", "high")
