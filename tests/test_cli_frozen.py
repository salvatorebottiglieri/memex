"""CLI subprocess tests for `check`, `freeze`, `thaw` and frozen visibility."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from tests.conftest import _q, _run_memex, register_node


def _memex(store: dict, *args: str):
    return _run_memex(
        [*args, "--db", str(store["db"]), "--vault", str(store["vault"])]
    )


def _registered_node(store: dict) -> dict:
    result = register_node(
        store, store["vault"], "article.md", "https://example.com/article"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _list_ids(store: dict, *extra: str) -> list[str]:
    result = _memex(store, "list", *extra)
    assert result.returncode == 0, result.stderr
    return [node["id"] for node in json.loads(result.stdout)]


def test_check_reports_missing_content_path(store):
    node = _registered_node(store)
    node_id = node["id"]

    listed = json.loads(_memex(store, "list").stdout)
    content_path = next(n["content_path"] for n in listed if n["id"] == node_id)
    Path(content_path).unlink()

    result = _memex(store, "check")

    assert result.returncode == 1
    findings = json.loads(result.stdout)
    assert any(
        f["code"] == "missing_content" and f["node_id"] == node_id
        for f in findings
    )


def test_check_reports_dangling_provenance(store):
    node = _registered_node(store)
    node_id = node["id"]
    # The CLI-level Store enforces FK; insert through a plain connection with
    # foreign_keys off to simulate a locator that a legacy/manual write left
    # pointing at a node that no longer exists.
    _q(
        store,
        "INSERT INTO edge (id, type, relation, from_node, to_node, written_by) "
        "VALUES (?, 'provenance', 'derived_from', ?, ?, 'human')",
        ("edge-dangling", node_id, "ghost-node"),
    )

    result = _memex(store, "check")

    assert result.returncode == 1
    findings = json.loads(result.stdout)
    assert any(
        f["code"] == "dangling_provenance" and f["node_id"] == node_id
        for f in findings
    )


def test_check_is_clean_on_a_healthy_vault(store):
    _registered_node(store)

    result = _memex(store, "check")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_freeze_hides_from_list_and_search_then_thaw_restores(store):
    node = _registered_node(store)
    node_id = node["id"]

    frozen = _memex(store, "freeze", node_id, "--reason", "content file deleted")

    assert frozen.returncode == 0, frozen.stderr
    assert json.loads(frozen.stdout)["status"] == "frozen"

    # list: hidden by default, visible only with the explicit flag
    assert node_id not in _list_ids(store)
    assert node_id in _list_ids(store, "--include-frozen")
    with_frozen = json.loads(_memex(store, "list", "--include-frozen").stdout)
    frozen_node = next(n for n in with_frozen if n["id"] == node_id)
    assert frozen_node["frozen_at"] is not None
    assert frozen_node["frozen_reason"] == "content file deleted"

    # search: the frozen derivation-content node is excluded
    search = _memex(store, "search", "longer")
    assert search.returncode == 0, search.stderr
    assert node_id not in {r["id"] for r in json.loads(search.stdout)}
    search_with_frozen = _memex(store, "search", "longer", "--include-frozen")
    assert node_id in {r["id"] for r in json.loads(search_with_frozen.stdout)}

    # thaw: back in the default list and searchable again
    thawed = _memex(store, "thaw", node_id)
    assert thawed.returncode == 0, thawed.stderr
    assert json.loads(thawed.stdout)["status"] == "thawed"
    assert node_id in _list_ids(store)
    search_again = _memex(store, "search", "longer")
    assert node_id in {r["id"] for r in json.loads(search_again.stdout)}


def test_freeze_unknown_node_fails(store):
    result = _memex(store, "freeze", "no-such-node", "--reason", "missing")

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "not_found"


def test_thaw_unknown_node_fails(store):
    result = _memex(store, "thaw", "no-such-node")

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"] == "not_found"


def test_thaw_not_frozen_node_is_idempotent(store):
    node = _registered_node(store)

    result = _memex(store, "thaw", node["id"])

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "not_frozen"


def test_sync_reports_frozen_count(store):
    node = _registered_node(store)
    result = _memex(store, "freeze", node["id"], "--reason", "source gone")
    assert result.returncode == 0, result.stderr

    vault = store["vault"]
    os.system(f"git -C {shlex.quote(str(vault))} init -q")
    os.system(f"git -C {shlex.quote(str(vault))} add -A")
    os.system(f"git -C {shlex.quote(str(vault))} commit -q -m init")

    result = _memex(store, "sync", "--no-push")

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["frozen"] == 1


def test_store_freeze_filters_list_nodes(db_store):
    db_store.create_node(
        node_id="n1",
        kind="raw_source",
        trust_state="draft",
        depth=0,
        content_path=None,
        created_at="2026-01-01T00:00:00+00:00",
    )

    assert db_store.mark_frozen(node_id="n1", reason="gone", at="2026-01-02T00:00:00+00:00") == 1
    assert db_store.list_nodes() == []
    assert [n["id"] for n in db_store.list_nodes(include_frozen=True)] == ["n1"]

    node = db_store.get_node("n1")
    assert node is not None
    assert node["frozen_at"] == "2026-01-02T00:00:00+00:00"
    assert node["frozen_reason"] == "gone"
    assert db_store.get_stats()["frozen"] == 1

    assert db_store.clear_frozen(node_id="n1") == 1
    assert db_store.clear_frozen(node_id="n1") == 0
    assert [n["id"] for n in db_store.list_nodes()] == ["n1"]
    assert db_store.mark_frozen(node_id="missing", reason="x", at="now") == 0
