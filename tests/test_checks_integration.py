"""Integration tests: checks module wired into the derive command and surfaced in show/list.

Tests exercise the CLI seam — no mocking of internals.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from memex.store import Store
from tests.conftest import _run_memex


FAKE_AGENT = "tests.fake_llm_client:FakeAgent"
FAKE_FAILING_AGENT = "tests.fake_llm_client_failing:FakeLLMClientFailing"


def _ingest(store, url: str) -> dict:
    """Create a legacy raw_source L0 directly via the Store (transition phase).

    ``memex register`` now produces url+extracted pairs; derive + checks
    still process legacy raw_source nodes. The depth-0 L0 shape is what keeps
    the passing-derivation assertions valid (a notes derivation of a depth-0
    L0 lands at depth 1, so D5's parent-depth+1 expectation passes and the
    node auto-verifies). Seed those fixtures directly to exercise that path.
    """
    filename = url.rsplit("/", 1)[-1].split("?", 1)[0] + ".md"
    md_path = store["vault"] / filename
    md_path.write_text(
        f"---\nsource_url: {url}\ntitle: Test Article\n---\n\n"
        f"# Test Article\n\n"
        f"This is a longer article body that exceeds the minimum character threshold "
        f"of one hundred characters so that the L0 markdown file gets created in tests.",
        encoding="utf-8",
    )
    con = sqlite3.connect(store["db"])
    st = Store(con)
    node_id = str(uuid.uuid4())
    st.create_node(
        node_id=node_id, kind="raw_source", depth=0,
        content_path=str(md_path), created_at=datetime.now(timezone.utc).isoformat(),
    )
    st.attach_source(
        node_id=node_id, canonical_key=url,
        source_url=url, title="Test Article", fetched_at=None,
    )
    con.commit()
    con.close()
    return {"id": node_id}


def _derive(store, node_id: str, agent_module: str = FAKE_AGENT):
    return _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": agent_module},
    )


def _show(store, node_id: str):
    return _run_memex(
        ["show", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
    )


class TestPassingDerivation:
    def test_passing_derivation_is_auto_verified(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        data = json.loads(result.stdout)
        assert data["trust_state"] == "auto-verified"
        assert data["check_failures"] == []

    def test_passing_derivation_shows_no_failures(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"])
        deriv_id = json.loads(result.stdout)["id"]
        show = _show(store, deriv_id)
        data = json.loads(show.stdout)
        assert data["trust_state"] == "auto-verified"
        assert data["check_failures"] == []


class TestFailingDerivation:
    def test_failing_derivation_stays_draft(self, store):
        """FakeLLMClientFailing produces a derivation without > Synthesis: marker
        and shorter than MIN_CHARS, so the node stays in draft."""
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"], agent_module=FAKE_FAILING_AGENT)
        data = json.loads(result.stdout)
        assert data["trust_state"] == "draft"
        assert len(data["check_failures"]) >= 1

    def test_failing_derivation_failures_are_persisted(self, store):
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"], agent_module=FAKE_FAILING_AGENT)
        deriv_id = json.loads(result.stdout)["id"]

        show = _show(store, deriv_id)
        data = json.loads(show.stdout)
        assert data["trust_state"] == "draft"
        assert isinstance(data["check_failures"], list)
        assert len(data["check_failures"]) >= 1

    def test_failing_derivation_failures_in_db(self, store):
        """The check_failures JSON column on node is populated for draft derivations."""
        ingested = _ingest(store, "https://example.com/article")
        result = _derive(store, ingested["id"], agent_module=FAKE_FAILING_AGENT)
        deriv_id = json.loads(result.stdout)["id"]

        con = sqlite3.connect(store["db"])
        row = con.execute(
            "SELECT trust_state, check_failures FROM node WHERE id = ?", (deriv_id,)
        ).fetchone()
        con.close()
        assert row[0] == "draft"
        assert row[1] is not None  # JSON string, not NULL
        failures = json.loads(row[1])
        assert len(failures) >= 1


class TestListShowsDerivation:
    def test_list_includes_summary_nodes(self, store):
        ingested = _ingest(store, "https://example.com/article")
        _derive(store, ingested["id"])
        result = _run_memex(
            ["list", "--db", str(store["db"]), "--vault", str(store["vault"])],
        )
        data = json.loads(result.stdout)
        kinds = {row["kind"] for row in data}
        assert "raw_source" in kinds
        assert "summary" in kinds