"""Tests for `memex revalidate` — re-judges an existing node's state columns.

The command's whole point (#171) is that the markdown file is never written:
the byte + mtime assertions are what catch a rewrite. Judges are the fakes of
``tests/fake_llm_client.py`` — no network, no real LLM (same family as
``test_validate_derivation.py``).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memex.services.revalidate import RevalidatorService
from memex.store import Store
from tests.conftest import _run_memex
from tests.fake_llm_client import FakeJudge

FAKE_JUDGE = "tests.fake_llm_client:FakeJudge"

# Two sentences the node can quote verbatim: the grounded one is token-covered
# by the parent (D7), the other carries the fake judge's UNSUPPORTED sentinel.
_PARENT = (
    "The memex system derives notes from source documents and records "
    "provenance edges. The source material covers the subject thoroughly."
)
_GROUNDED_CLAIM = (
    "The memex system derives notes from source documents and records "
    "provenance edges."
)
_SYNTHESIS_STATEMENT = "The system keeps provenance edges for every note."
_GROUNDED_PROSE = (
    "# Notes\n\n"
    f"{_GROUNDED_CLAIM} The source material covers the subject thoroughly.\n\n"
    f"> Synthesis: {_SYNTHESIS_STATEMENT}\n"
)
_UNGROUNDED_PROSE = (
    "# Notes\n\n"
    "SENTINEL-UNSUPPORTED the vendor reported 4200 breaking changes in 2024.\n\n"
    f"{_GROUNDED_CLAIM}\n\n"
    f"> Synthesis: {_SYNTHESIS_STATEMENT}\n"
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary_node(
    store: Store,
    root: Path,
    stem: str,
    *,
    prose: str,
    trust_state: str = "auto-verified",
    check_failures: list[str] | None = None,
) -> tuple[str, Path]:
    """A notes-tier summary node with one readable parent, in *trust_state*."""
    parent_path = root / f"{stem}-parent.md"
    parent_path.write_text(_PARENT, encoding="utf-8")
    parent_id = str(uuid.uuid4())
    store.create_node(
        node_id=parent_id, kind="raw_source", depth=0,
        content_path=str(parent_path), created_at=_utcnow(),
        trust_state="auto-verified", confidence="medium",
    )
    node_path = root / f"{stem}.md"
    node_path.write_text(prose, encoding="utf-8")
    node_id = str(uuid.uuid4())
    store.create_node(
        node_id=node_id, kind="summary", tier="notes", depth=1,
        content_path=str(node_path), created_at=_utcnow(),
        trust_state=trust_state, synthesis_statements=[_SYNTHESIS_STATEMENT],
    )
    store.create_edge(
        edge_id=str(uuid.uuid4()), type="provenance", relation="derived_from",
        from_node=node_id, to_node=parent_id,
    )
    store.update_trust_state(
        node_id=node_id,
        trust_state=trust_state,
        check_failures=[] if check_failures is None else check_failures,
    )
    return node_id, node_path


def _state(db_path: Path, node_id: str) -> tuple[str, list[str] | None]:
    with Store.open(db_path) as store:
        node = store.get_node(node_id)
    assert node is not None
    return node["trust_state"], node["check_failures"]


def _revalidate(store: dict, *args: str):
    return _run_memex(
        ["revalidate", *args, "--db", str(store["db"]), "--vault", str(store["vault"])],
        env={"MEMEX_AGENT": FAKE_JUDGE},
    )


def test_ungrounded_node_demotes_and_the_markdown_is_untouched(tmp_path):
    """AC1: auto-verified → draft with annotations, file byte- and mtime-identical."""
    db_path = tmp_path / "memex.db"
    with Store.open(db_path) as store:
        store.init_schema()
        node_id, node_path = _summary_node(
            store, tmp_path, "node", prose=_UNGROUNDED_PROSE
        )
    before_bytes = node_path.read_bytes()
    before_mtime = node_path.stat().st_mtime_ns

    with Store.open(db_path) as store:
        result = RevalidatorService(store, FakeJudge()).revalidate(node_id)

    assert result.status == "revalidated"
    assert result.previous_trust_state == "auto-verified"
    assert result.trust_state == "draft"
    assert result.demoted is True
    assert result.check_failures
    assert any("V1" in f for f in result.check_failures)
    assert _state(db_path, node_id) == ("draft", result.check_failures)
    # The command's contract: prose is never rewritten.
    assert node_path.read_bytes() == before_bytes
    assert node_path.stat().st_mtime_ns == before_mtime


def test_accepted_node_keeps_its_trust_state_and_failures(tmp_path):
    """AC2: the current gate still accepts it → nothing changes."""
    db_path = tmp_path / "memex.db"
    with Store.open(db_path) as store:
        store.init_schema()
        node_id, node_path = _summary_node(
            store, tmp_path, "node", prose=_GROUNDED_PROSE
        )
    before_mtime = node_path.stat().st_mtime_ns

    with Store.open(db_path) as store:
        result = RevalidatorService(store, FakeJudge()).revalidate(node_id)

    assert result.trust_state == "auto-verified"
    assert result.demoted is False
    assert result.check_failures == []
    assert _state(db_path, node_id) == ("auto-verified", [])
    assert node_path.stat().st_mtime_ns == before_mtime


def test_two_consecutive_runs_give_the_same_result(tmp_path):
    """AC5: idempotent — the second run is a no-op with the same verdict."""
    db_path = tmp_path / "memex.db"
    with Store.open(db_path) as store:
        store.init_schema()
        node_id, _ = _summary_node(store, tmp_path, "node", prose=_UNGROUNDED_PROSE)

    with Store.open(db_path) as store:
        first = RevalidatorService(store, FakeJudge()).revalidate(node_id)
    with Store.open(db_path) as store:
        second = RevalidatorService(store, FakeJudge()).revalidate(node_id)

    assert (first.trust_state, first.check_failures) == (
        second.trust_state,
        second.check_failures,
    )
    assert second.demoted is False


def test_all_limit_visits_one_node_and_prints_a_summary(store):
    """AC3/AC4: --all --limit 1 touches exactly one node, exit 1 on worsening."""
    db_path = store["db"]
    with Store.open(db_path) as s:
        s.init_schema()
        first, _ = _summary_node(s, store["vault"], "a", prose=_UNGROUNDED_PROSE)
        second, _ = _summary_node(s, store["vault"], "b", prose=_UNGROUNDED_PROSE)

    results = _revalidate(store, "--all", "--limit", "1")

    assert results.returncode == 1, results.stderr
    lines = [json.loads(line) for line in results.stdout.splitlines() if line.strip()]
    assert len(lines) == 2  # one node + the summary
    node_line, summary = lines
    assert summary["revalidated"] == 1
    assert summary["demoted"] == 1
    assert summary["regressed"] is True

    # Exactly one of the two candidates was visited (both would demote).
    states = sorted(_state(db_path, nid)[0] for nid in (first, second))
    assert states == ["auto-verified", "draft"]
    assert _state(db_path, node_line["id"])[0] == "draft"


def test_all_exits_zero_when_no_node_worsens(store):
    """AC4: nothing regressed → exit 0."""
    db_path = store["db"]
    with Store.open(db_path) as s:
        s.init_schema()
        _summary_node(s, store["vault"], "node", prose=_GROUNDED_PROSE)

    results = _revalidate(store, "--all")

    assert results.returncode == 0, results.stderr
    summary = json.loads(results.stdout.splitlines()[-1])
    assert (summary["revalidated"], summary["demoted"], summary["regressed"]) == (
        1,
        0,
        False,
    )
