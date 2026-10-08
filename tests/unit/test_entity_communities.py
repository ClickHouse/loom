"""Communities over the entity graph: deterministic label
propagation, per-community capping of graph expansion, the worker pass and
the taxonomy read."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from server.memory.app.entity_communities import (
    EntityCommunitiesConfig,
    label_propagation,
    rows_for_namespace,
    run_entity_communities,
    summarize_communities,
)


def _clique(names: list[str], w: float = 1.0) -> list[tuple[str, str, float]]:
    return [(a, b, w) for i, a in enumerate(names) for b in names[i + 1 :]]


def test_two_cliques_joined_by_a_bridge_are_two_communities() -> None:
    a = ["alice", "bob", "carol", "dave"]
    b = ["clickhouse", "loom", "staging", "otel"]
    edges = _clique(a) + _clique(b) + [("dave", "clickhouse", 1.0)]
    assignment = label_propagation(edges)
    assert len({assignment[n] for n in a}) == 1
    assert len({assignment[n] for n in b}) == 1
    assert assignment["alice"] != assignment["loom"]
    # ids renumbered by size desc then smallest member: both size 4 → "alice" < "clickhouse"
    assert assignment["alice"] == 0 and assignment["loom"] == 1


def test_weights_decide_membership_and_output_is_deterministic() -> None:
    # "x" touches both cliques; the heavier tie wins.
    a = ["a1", "a2", "a3"]
    b = ["b1", "b2", "b3"]
    edges = _clique(a) + _clique(b) + [("x", "a1", 1.0), ("x", "b1", 5.0), ("x", "b2", 5.0)]
    one = label_propagation(edges)
    two = label_propagation(list(reversed(edges)))
    assert one == two
    assert one["x"] == one["b1"] != one["a1"]


def test_self_loops_zero_weights_and_empty_graph() -> None:
    assert label_propagation([]) == {}
    assert label_propagation([("a", "a", 3.0), ("b", "c", 0.0)]) == {}
    assert label_propagation([("a", "b", 1.0)]) == {"a": 0, "b": 0}


def test_summaries_label_by_highest_degree_member() -> None:
    edges = [("hub", "x", 1.0), ("hub", "y", 1.0), ("hub", "z", 1.0), ("x", "y", 1.0)]
    assignment = label_propagation(edges)
    degree = {"hub": 3, "x": 2, "y": 2, "z": 1}
    comms = summarize_communities(assignment, degree)
    assert len(comms) == 1 and comms[0].label == "hub" and comms[0].size == 4
    assert comms[0].members[:2] == ("hub", "x")  # degree desc, then name


def test_rows_for_namespace_shape() -> None:
    t0 = datetime(2026, 9, 11, 20, 0, 0)
    rows, comms = rows_for_namespace("o", "n", _clique(["a", "b", "c"]), computed_at=t0)
    assert [r["entity"] for r in rows] == ["a", "b", "c"]
    assert all(r["community_id"] == 0 and r["size"] == 3 and r["level"] == 0 for r in rows)
    assert rows[0]["label"] == "a" and rows[0]["degree"] == 2 and rows[0]["computed_at"] == t0
    assert len(comms) == 1


class _StubCH:
    def __init__(
        self, namespaces: list[dict[str, Any]], edges: dict[str, list[dict[str, Any]]]
    ) -> None:
        self.namespaces = namespaces
        self.edges = edges
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.queries.append((sql, params or {}))
        if "SELECT DISTINCT e.org" in sql:
            return self.namespaces
        if "FROM loom.entity_edges" in sql:
            return self.edges.get(params["ns"], [])
        return []

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows))


@pytest.mark.asyncio
async def test_worker_pass_writes_one_row_per_entity_and_truncates_oversized() -> None:
    """An oversized graph keeps its strongest max_edges pairs
    (the stub hands back the +1 row the read asks for) instead of being
    skipped."""
    ch = _StubCH(
        namespaces=[{"org": "o", "namespace": "small"}, {"org": "o", "namespace": "huge"}],
        edges={
            "small": [
                {"entity_a": "a", "entity_b": "b", "w": 3},
                {"entity_a": "b", "entity_b": "c", "w": 1},
            ],
            "huge": [{"entity_a": f"e{i}", "entity_b": f"e{i + 1}", "w": 1} for i in range(6)],
        },
    )
    res = await run_entity_communities(
        EntityCommunitiesConfig(max_edges_per_namespace=5, walked_days=0), ch=ch
    )
    assert res.namespaces == 2 and res.rows_written == 3 + 6
    assert res.truncated == {"o/huge": True}
    table, rows = ch.inserted[0]
    assert table == "loom.entity_communities"
    assert {r["entity"] for r in rows} == {"a", "b", "c"}
    # huge: six pairs read, the weakest (e5–e6) cut → entities e0..e5
    _table, huge_rows = ch.inserted[1]
    assert {r["entity"] for r in huge_rows} == {f"e{i}" for i in range(6)}
    assert res.per_namespace == {"o/small": 1, "o/huge": 1}
    # the edge scan is current-edges-only and weight-summed
    edge_sql = [q for q, _ in ch.queries if "sum(co_occur)" in q][0]
    assert "invalid_at IS NULL" in edge_sql and "sum(co_occur) AS w" in edge_sql


@pytest.mark.asyncio
async def test_worker_pass_partitions_off_the_event_loop() -> None:
    """Hub drop + label propagation is seconds of CPU on a 200k-edge
    namespace; run inline it stalls every request and worker sharing the
    loop. The partition must run in a thread with no running loop."""
    from server.memory.app import entity_communities as svc

    real = svc._partition
    seen: list[bool] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            seen.append(True)
        except RuntimeError:
            seen.append(False)
        return real(*args, **kwargs)

    ch = _StubCH(
        namespaces=[{"org": "o", "namespace": "n"}],
        edges={"n": [{"entity_a": "a", "entity_b": "b", "w": 2}]},
    )
    with patch.object(svc, "_partition", spy):
        res = await run_entity_communities(EntityCommunitiesConfig(walked_days=0), ch=ch)
    assert seen == [False]
    assert res.rows_written == 2


def test_worker_is_registered_default_off() -> None:
    from server.settings import settings
    from server.workers.registry import worker_meta

    assert settings.entity_communities_enabled is False
    snap = worker_meta()
    assert "entity_communities" in snap and snap["entity_communities"]["enabled"] is False


def test_taxonomy_surfaces_communities_and_tolerates_none(monkeypatch) -> None:
    from server.identity.api.models_namespace_catalog import NamespaceTaxonomyInput
    from server.identity.app import namespace_catalog as h

    class _CH:
        async def query_all(self, sql, params=None):
            if "loom.entity_communities" in sql:
                return [
                    {
                        "community_id": 0,
                        "label": "clickhouse",
                        "size": 9,
                        "top_entities": ["clickhouse", "loom", "staging"],
                        "computed_at": datetime(2026, 9, 11, 20, 0, 0),
                    },
                    {
                        "community_id": 1,
                        "label": "alice",
                        "size": 4,
                        "top_entities": ["alice", "bob"],
                        "computed_at": datetime(2026, 9, 11, 20, 0, 0),
                    },
                ]
            return [{"kind": "count", "value": "", "count": 12}]

    with patch.object(h, "get_client", return_value=_CH()):
        out = asyncio.run(
            h.handle_namespace_taxonomy(NamespaceTaxonomyInput(org="o", namespace="n"))
        )
        assert [c.label for c in out.communities] == ["clickhouse", "alice"]
        assert out.communities[0].top_entities == ["clickhouse", "loom", "staging"]
        assert out.communities_computed_at.startswith("2026-09-11")
        off = asyncio.run(
            h.handle_namespace_taxonomy(
                NamespaceTaxonomyInput(org="o", namespace="n", top_n_communities=0)
            )
        )
        assert off.communities == [] and off.communities_computed_at == ""


@pytest.mark.asyncio
async def test_namespace_scan_joins_the_opt_in_flag() -> None:
    """The pass covers the opted-in namespaces."""
    from server.memory.app.entity_communities import _namespaces_with_edges

    class _CH:
        def __init__(self) -> None:
            self.sqls: list[str] = []

        async def query_all(self, sql, params=None):
            self.sqls.append(sql)
            return [{"org": "o", "namespace": "n"}]

    # walked_days 0: the opted-in namespaces statement alone
    ch = _CH()
    got = await _namespaces_with_edges(ch, EntityCommunitiesConfig(walked_days=0))
    assert got == [("o", "n")] and len(ch.sqls) == 1
    assert "loom.namespaces" in ch.sqls[0] and "consolidation_enabled = true" in ch.sqls[0]
    ch = _CH()
    await _namespaces_with_edges(ch, EntityCommunitiesConfig(walked_days=0, org_filter="o"))
    assert len(ch.sqls) == 1 and "e.org = {org:String}" in ch.sqls[0]
    ch = _CH()
    await _namespaces_with_edges(ch, EntityCommunitiesConfig(namespace_filter="n", walked_days=0))
    assert "e.namespace = {ns:String}" in ch.sqls[0]


def test_drop_hubs_removes_the_star_centre_and_its_edges() -> None:
    """A hub joined to every cluster leaves before propagation."""
    from server.memory.app.entity_communities import drop_hubs

    clique_a = [("a1", "a2", 3.0), ("a2", "a3", 3.0), ("a1", "a3", 3.0)]
    clique_b = [("b1", "b2", 3.0), ("b2", "b3", 3.0), ("b1", "b3", 3.0)]
    hub = [("hub", n, 1.0) for n in ("a1", "a2", "a3", "b1", "b2", "b3")]
    edges = clique_a + clique_b + hub
    # seven entities; 0.2% floors to zero → nothing dropped
    kept, hubs = drop_hubs(edges, 0.002)
    assert kept == edges and hubs == []
    # a fraction that reaches one entity drops the hub and its six edges
    kept, hubs = drop_hubs(edges, 0.15)
    assert hubs == ["hub"] and len(kept) == 6 and all("hub" not in (a, b) for a, b, _ in kept)
    # with the hub gone the two cliques are two communities
    assert len(set(label_propagation(kept).values())) == 2
    assert drop_hubs(edges, 0.0) == (edges, []) and drop_hubs([], 0.5) == ([], [])


@pytest.mark.asyncio
async def test_worker_pass_records_the_hubs_it_dropped() -> None:
    star = [("hub", f"n{i}", 1.0) for i in range(10)] + [("n0", "n1", 2.0), ("n2", "n3", 2.0)]
    ch = _StubCH(
        namespaces=[{"org": "o", "namespace": "s"}],
        edges={"s": [{"entity_a": a, "entity_b": b, "w": w} for a, b, w in star]},
    )
    res = await run_entity_communities(
        EntityCommunitiesConfig(walked_days=0, hub_drop_fraction=0.1), ch=ch
    )
    assert res.hubs_dropped == {"o/s": ["hub"]}
    _table, rows = ch.inserted[0]
    assert "hub" not in {r["entity"] for r in rows}


def test_hub_drop_default_is_the_top_fraction_of_a_percent() -> None:
    from server.settings import settings

    assert settings.entity_communities_hub_drop_fraction == 0.002
