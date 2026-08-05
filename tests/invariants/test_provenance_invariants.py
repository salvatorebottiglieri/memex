"""Core invariants — provenance DAG (vertical load-bearing, ADR-0002/0005).

    P1  every node with depth>0 has >=1 outgoing derived_from edge
        (child -> parent: from_node = the derived node)
    P2  every derived_from target exists (no dangling references, rule D2)
    P3  child.depth == max(parent.depth) + 1 for every derived_from edge
    P4  the provenance graph is acyclic; url nodes (depth 0) never emit
        outgoing derived_from edges
    P5  every summary/synthesis traces transitively to at least one source
        row (the ground floor — vertical load-bearing)
    P6  L0 depth model: url=0, extracted=1, notes=2, synthesis>=3

All assertions are read through direct SQLite reads after building the graph
through the CLI (register -> derive -> synthesize) with the fake agent.
"""
from __future__ import annotations

import json
from collections import deque

from tests.conftest import _q, _run_memex, register_node

FAKE_AGENT = "tests.fake_llm_client:FakeAgent"


def _derive(store, node_id: str):
    result = _run_memex(
        ["derive", "--db", str(store["db"]), "--vault", str(store["vault"]), node_id],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _synthesize(store, *node_ids: str):
    result = _run_memex(
        ["synthesize", "--db", str(store["db"]), "--vault", str(store["vault"]), *node_ids],
        env={"MEMEX_AGENT": FAKE_AGENT},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _build_campus(store) -> dict:
    """Register 2 sources, derive both, synthesize across both summaries."""
    a = json.loads(register_node(store, store["vault"], "a.md", "https://example.com/a").stdout)
    b = json.loads(register_node(store, store["vault"], "b.md", "https://example.com/b").stdout)
    da = _derive(store, a["id"])
    db = _derive(store, b["id"])
    assert da["status"] == "derived" and db["status"] == "derived"
    s = _synthesize(store, da["id"], db["id"])
    assert s["status"] == "synthesized"
    return {"a": a, "b": b, "da": da, "db": db, "s": s}


class TestProvenanceEdges:
    def test_every_node_above_l0_has_incoming_derived_from(self, store):
        _build_campus(store)
        nodes = _q(store, "SELECT id, depth FROM node")
        for node_id, depth in nodes:
            if depth <= 0:
                continue
            edges = _q(
                store,
                "SELECT COUNT(*) FROM edge WHERE relation='derived_from' AND from_node=?",
                (node_id,),
            )
            assert edges[0][0] >= 1, f"node {node_id} (depth {depth}) has no provenance"

    def test_no_dangling_provenance_targets(self, store):
        _build_campus(store)
        targets = _q(store, "SELECT DISTINCT to_node FROM edge WHERE relation='derived_from'")
        existing = {row[0] for row in _q(store, "SELECT id FROM node")}
        for (target,) in targets:
            assert target in existing, f"dangling derived_from target {target}"

    def test_url_nodes_never_emit_derived_from(self, store):
        _build_campus(store)
        url_ids = [row[0] for row in _q(store, "SELECT id FROM node WHERE kind='url'")]
        for url_id in url_ids:
            outgoing = _q(
                store,
                "SELECT COUNT(*) FROM edge WHERE relation='derived_from' AND from_node=?",
                (url_id,),
            )
            assert outgoing[0][0] == 0, f"url node {url_id} emits derived_from edges"


class TestDepthModel:
    def test_depth_equals_max_parent_plus_one(self, store):
        """P3 — child.depth == max(parent.depth)+1.

        A synthesis may rest on parents at different floors (notes d2 +
        synthesis d3 -> d4); the depth tracks the deepest parent.
        """
        _build_campus(store)
        rows = _q(
            store,
            "SELECT e.from_node, c.depth, p.depth FROM edge e "
            "JOIN node c ON c.id = e.from_node "
            "JOIN node p ON p.id = e.to_node "
            "WHERE e.relation='derived_from'",
        )
        by_child: dict[str, list] = {}
        for child_id, child_depth, parent_depth in rows:
            by_child.setdefault(child_id, []).append((child_depth, parent_depth))
        assert by_child, "no provenance edges found"
        for child_id, pairs in by_child.items():
            child_depth = pairs[0][0]
            max_parent = max(pd for _, pd in pairs)
            assert child_depth == max_parent + 1, (
                f"node {child_id}: depth {child_depth} != max parent depth {max_parent} + 1"
            )

    def test_l0_depth_model(self, store):
        _build_campus(store)
        depths = {
            kind: {row[0]: row[1] for row in _q(store, "SELECT id, depth FROM node WHERE kind=?", (kind,))}
            for kind in ("url", "extracted", "summary")
        }
        assert set(depths["url"].values()) == {0}
        assert set(depths["extracted"].values()) == {1}
        assert set(depths["summary"].values()) >= {2, 3}  # notes=2, synthesis=3

    def test_mixed_depth_parents_land_at_max_plus_one(self, store):
        """Corpus-verified: notes(d2) + synthesis(d3) -> depth 4."""
        campus = _build_campus(store)
        # A notes node and the synthesis node as parents: max depth = 3 -> 4.
        s = _synthesize(store, campus["da"]["id"], campus["s"]["id"])
        assert s["status"] == "synthesized"
        (depth,) = _q(store, "SELECT depth FROM node WHERE id=?", (s["id"],))[0]
        assert depth == 4

    def test_provenance_graph_is_acyclic(self, store):
        _build_campus(store)
        children = {}
        for (from_node, to_node) in _q(
            store, "SELECT from_node, to_node FROM edge WHERE relation='derived_from'"
        ):
            children.setdefault(from_node, []).append(to_node)

        for start in children:
            seen_in_path = {start}
            stack = list(children[start])
            while stack:
                node = stack.pop()
                assert node not in seen_in_path, (
                    f"cycle detected in provenance graph at {node}"
                )
                seen_in_path.add(node)
                stack.extend(children.get(node, []))


class TestVerticalLoadBearing:
    def test_every_derivation_traces_to_a_source_row(self, store):
        _build_campus(store)
        nodes = _q(store, "SELECT id, depth FROM node WHERE depth > 0")
        parents = {}
        for (from_node, to_node) in _q(
            store, "SELECT from_node, to_node FROM edge WHERE relation='derived_from'"
        ):
            parents.setdefault(from_node, []).append(to_node)

        for node_id, _ in nodes:
            queue = deque(parents.get(node_id, []))
            visited = {node_id}
            reached_source = False
            while queue:
                current = queue.popleft()
                if current in visited:
                    continue
                visited.add(current)
                has_source = _q(store, "SELECT COUNT(*) FROM source WHERE node_id=?", (current,))
                if has_source[0][0] > 0:
                    reached_source = True
                    break
                queue.extend(parents.get(current, []))
            assert reached_source, f"node {node_id} does not trace to any source row"
