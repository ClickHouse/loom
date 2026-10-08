"""A graph answer shows its path.

CH-free: the pure path helpers, the neighbour fetch filling its paths map
from `via_seed`, facts.traverse projecting the CTE's visited chain, and
the graph-mode hit carrying `via_path`.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from server.memory.domain.graph_paths import extend_paths, via_path

# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_extend_paths_chains_hops_and_keeps_first_route() -> None:
    paths: dict[str, list[str]] = {}
    extend_paths(paths, [("acme", "alice"), ("bob", "alice")], ["alice"])
    extend_paths(paths, [("carol", "acme"), ("bob", "acme")], ["alice"])  # bob already routed
    assert paths == {
        "acme": ["alice", "acme"],
        "bob": ["alice", "bob"],
        "carol": ["alice", "acme", "carol"],
    }


def test_extend_paths_never_leaves_a_neighbour_without_a_route() -> None:
    paths: dict[str, list[str]] = {}
    # Stubbed rows carry no via_seed; a seed itself is never a neighbour.
    extend_paths(paths, [("clickhouse", ""), ("alice", "")], ["alice", "bob"])
    assert paths == {"clickhouse": ["alice", "clickhouse"]}


def test_via_path_prefers_a_seed_on_the_hit_then_the_shortest_chain() -> None:
    paths = {"acme": ["alice", "acme"], "carol": ["alice", "acme", "carol"]}
    assert via_path(["carol", "alice"], ["alice"], paths) == ["alice"]
    assert via_path(["carol", "acme"], ["alice"], paths) == ["alice", "acme"]
    assert via_path(["carol"], ["alice"], paths) == ["alice", "acme", "carol"]
    assert via_path(["zed"], ["alice"], paths) == []
    assert via_path([], ["alice"], paths) == []


# ----------------------------------------------------------------------
# fetch_graph_neighbors fills the paths map
# ----------------------------------------------------------------------


class _CH:
    def __init__(self, hops: list[list[dict[str, Any]]]) -> None:
        self.hops = hops
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if "FROM loom.entity_degree" in sql:
            return []
        return self.hops.pop(0) if self.hops else []


@pytest.mark.asyncio
async def test_fetch_graph_neighbors_records_the_route_per_neighbour() -> None:
    from server.memory.app.retrieval import fetch_graph_neighbors

    ch = _CH(
        [
            [
                {"neighbor": "acme", "weight": 5, "via_seed": "alice"},
                {"neighbor": "bob", "weight": 2, "via_seed": "alice"},
            ],
            [{"neighbor": "carol", "weight": 3, "via_seed": "acme"}],
        ]
    )
    paths: dict[str, list[str]] = {}
    out = await fetch_graph_neighbors(
        ch,
        org="o",
        namespace="n",
        seed_entities=["Alice"],
        hops=2,
        hub_skip_degree=0,
        paths=paths,
    )
    assert out == ["acme", "bob", "carol"]
    assert paths == {
        "acme": ["alice", "acme"],
        "bob": ["alice", "bob"],
        "carol": ["alice", "acme", "carol"],
    }
    sql, _ = ch.calls[0]
    assert "argMax(seed, w) AS via_seed" in sql  # `w`, not the shadowed alias
    # every union arm names the frontier entity it came from
    assert len(re.findall(r" AS seed\b", sql)) == 4


@pytest.mark.asyncio
async def test_fetch_graph_neighbors_return_value_is_unchanged_without_paths() -> None:
    from server.memory.app.retrieval import fetch_graph_neighbors

    ch = _CH([[{"neighbor": "acme"}]])
    out = await fetch_graph_neighbors(
        ch, org="o", namespace="n", seed_entities=["alice"], hub_skip_degree=0
    )
    assert out == ["acme"]


# ----------------------------------------------------------------------
# facts.traverse: path per node
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_facts_traverse_projects_the_visited_chain_as_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    class _StubCH:
        def __init__(self) -> None:
            self.sql = ""

        async def query_all(self, sql, params=None):

            # The hop SQL is what this test reads; the hub policy's degree

            # read (on by default) is a separate statement.

            if "frontier" in (params or {}):
                self.sql = sql
            frontier = (params or {}).get("frontier", [])
            if frontier == ["alice"]:
                return [
                    {
                        "src": "alice",
                        "dst": "acme",
                        "relation": "",
                        "direction": "",
                        "pair_co_occur": 4,
                        "provenance": "",
                    }
                ]
            if frontier == ["acme"]:
                return [
                    {
                        "src": "acme",
                        "dst": "carol",
                        "relation": "",
                        "direction": "",
                        "pair_co_occur": 2,
                        "provenance": "",
                    }
                ]
            return []

    ch = _StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)

    async def _no_communities(*_: Any, **__: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(
        "server.memory.app.entity_communities.community_assignment", _no_communities
    )
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity="Alice")
    )
    assert "NOT has({visited:Array(String)}, dst)" in ch.sql
    assert [n.path for n in out.nodes] == [["alice", "acme"], ["alice", "acme", "carol"]]
    assert all(n.hop == len(n.path) - 1 for n in out.nodes)


# ----------------------------------------------------------------------
# Graph-mode hit carries via_path; other modes do not
# ----------------------------------------------------------------------


def test_graph_via_path_only_in_graph_mode() -> None:
    from types import SimpleNamespace

    from server.memory.app.search.graph import _graph_via_path

    hit = SimpleNamespace(entities=["carol"])
    ctx = SimpleNamespace(
        input=SimpleNamespace(search_mode="graph"),
        effective_entity_filter=["alice"],
        graph_paths_by_ns={"n": {"carol": ["alice", "acme", "carol"]}},
    )
    assert _graph_via_path(ctx, "n", hit) == ["alice", "acme", "carol"]
    ctx.input.search_mode = "hybrid"
    assert _graph_via_path(ctx, "n", hit) == []


def test_wire_hit_declares_via_path_and_main_arm_projects_entities() -> None:
    from server.memory.api.models_search import _MemorySearchHit

    h = _MemorySearchHit(
        memory_key="k", memory_type="semantic", content_excerpt="x", distance=0.1, similarity=0.9
    )
    assert h.via_path == []
    src = open("server/memory/app/retrieval/fact_sql.py", encoding="utf-8").read()
    assert "e.entities AS entities," in src
