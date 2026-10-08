"""A namespace whose graph is walked gets its communities,
from its strongest edges.

The pass selects the union of the consolidation opt-ins and the
namespaces with a successful graph read (`facts.traverse` / `facts.related` /
`facts.path` / `namespace.taxonomy`) in `loom.agent_events` within
`walked_days`; an oversized graph keeps its strongest `max_edges` pairs and is
marked `truncated` instead of being skipped."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.entity_communities import (
    WALK_OPERATIONS,
    EntityCommunitiesConfig,
    _edges,
    _namespaces_with_edges,
    label_propagation,
    largest_share,
    run_entity_communities,
    summarize_communities,
)


class _CH:
    """Answers the opt-in scan and the walked scan separately and records
    every statement with its parameters."""

    def __init__(
        self,
        opted_in: list[dict[str, Any]],
        walked: list[dict[str, Any]],
        edges: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.opted_in = opted_in
        self.walked = walked
        self.edges = edges or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        if "FROM loom.agent_events" in sql:
            return self.walked
        if "consolidation_enabled = true" in sql:
            return self.opted_in
        if "sum(co_occur)" in sql:
            return self.edges.get(str((params or {})["ns"]), [])
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings=None) -> None:
        self.inserted.append((table, rows))


@pytest.mark.asyncio
async def test_selection_is_the_union_of_opted_in_and_walked() -> None:
    ch = _CH(
        opted_in=[{"org": "o", "namespace": "optin"}, {"org": "o", "namespace": "both"}],
        walked=[{"org": "o", "namespace": "both"}, {"org": "o", "namespace": "walked"}],
    )
    got = await _namespaces_with_edges(ch, EntityCommunitiesConfig(walked_days=7))
    # opt-ins first, walked additions after, no duplicate for a namespace in both
    assert got == [("o", "optin"), ("o", "both"), ("o", "walked")]
    assert len(ch.calls) == 2
    optin_sql, _ = ch.calls[0]
    walked_sql, walked_params = ch.calls[1]
    assert "consolidation_enabled = true" in optin_sql
    assert "FROM loom.agent_events AS a" in walked_sql
    # the ts range leads the predicate (agent_events is partitioned by month)
    where = walked_sql.split("WHERE", 1)[1]
    assert where.lstrip().startswith("a.ts >= now() - INTERVAL {days:UInt32} DAY")
    assert "a.operation IN {ops:Array(String)}" in walked_sql
    assert "a.success = true" in walked_sql and "n.active = true" in walked_sql
    assert "INNER JOIN loom.namespaces AS n FINAL" in walked_sql
    assert walked_params == {"days": 7, "ops": list(WALK_OPERATIONS)}
    assert set(WALK_OPERATIONS) == {
        "facts.traverse",
        "facts.related",
        "facts.path",
        "namespace.taxonomy",
    }


@pytest.mark.asyncio
async def test_walked_scan_composes_with_the_org_and_namespace_filters() -> None:
    ch = _CH(opted_in=[], walked=[{"org": "o", "namespace": "n"}])
    got = await _namespaces_with_edges(
        ch, EntityCommunitiesConfig(org_filter="o", namespace_filter="n")
    )
    assert got == [("o", "n")]
    walked_sql, walked_params = ch.calls[1]
    assert "a.org = {org:String}" in walked_sql and "a.namespace = {ns:String}" in walked_sql
    assert walked_params["org"] == "o" and walked_params["ns"] == "n"


@pytest.mark.asyncio
async def test_walked_scan_is_skipped_when_walked_days_is_zero() -> None:
    ch = _CH(opted_in=[{"org": "o", "namespace": "n"}], walked=[{"org": "o", "namespace": "w"}])
    assert await _namespaces_with_edges(ch, EntityCommunitiesConfig(walked_days=0)) == [("o", "n")]
    assert len(ch.calls) == 1 and "agent_events" not in ch.calls[0][0]


@pytest.mark.asyncio
async def test_edge_read_orders_by_weight_and_asks_for_one_past_the_cap() -> None:
    ch = _CH(opted_in=[], walked=[])
    await _edges(ch, "o", "n", EntityCommunitiesConfig(max_edges_per_namespace=10, min_co_occur=2))
    sql, params = ch.calls[0]
    assert "ORDER BY w DESC" in sql and "LIMIT {lim:UInt32}" in sql
    assert params == {"org": "o", "ns": "n", "min_co": 2, "lim": 11}


@pytest.mark.asyncio
async def test_oversized_graph_keeps_the_strongest_pairs_and_is_marked_truncated() -> None:
    # two cliques; the bridge is the weakest pair and the one past the cap
    strong = [
        {"entity_a": "a1", "entity_b": "a2", "w": 9},
        {"entity_a": "a2", "entity_b": "a3", "w": 8},
        {"entity_a": "b1", "entity_b": "b2", "w": 7},
        {"entity_a": "b2", "entity_b": "b3", "w": 6},
    ]
    bridge = [{"entity_a": "a3", "entity_b": "b1", "w": 1}]
    ch = _CH(
        opted_in=[{"org": "o", "namespace": "big"}, {"org": "o", "namespace": "fits"}],
        walked=[],
        edges={"big": strong + bridge, "fits": strong},
    )
    res = await run_entity_communities(
        EntityCommunitiesConfig(max_edges_per_namespace=4, hub_drop_fraction=0.0), ch=ch
    )
    assert res.truncated == {"o/big": True}
    assert res.namespaces == 2 and res.per_namespace == {"o/big": 2, "o/fits": 2}
    _t, big_rows = ch.inserted[0]
    by_entity = {r["entity"]: r["community_id"] for r in big_rows}
    # the cut bridge never joined the two cliques
    assert by_entity["a1"] == by_entity["a3"] != by_entity["b1"] == by_entity["b3"]
    assert set(by_entity) == {"a1", "a2", "a3", "b1", "b2", "b3"}


@pytest.mark.asyncio
async def test_truncation_is_logged_at_info_not_warning(caplog: pytest.LogCaptureFixture) -> None:
    pairs = [{"entity_a": f"e{i}", "entity_b": f"e{i + 1}", "w": 10 - i} for i in range(6)]
    ch = _CH(opted_in=[{"org": "o", "namespace": "big"}], walked=[], edges={"big": pairs})
    with caplog.at_level("INFO", logger="server.memory.app.entity_communities"):
        await run_entity_communities(
            EntityCommunitiesConfig(max_edges_per_namespace=5, hub_drop_fraction=0.0), ch=ch
        )
    records = [r for r in caplog.records if "strongest" in r.getMessage()]
    assert len(records) == 1 and records[0].levelname == "INFO"
    assert "o/big" in records[0].getMessage()


def _fused_graph() -> list[dict[str, Any]]:
    """Three cliques of eight (weight 1) and two hubs joined to every member
    at weight 10: the hubs out-vote the cliques and fuse the graph into one
    community; with both hubs gone it parts in three."""
    groups = [[f"{c}{i}" for i in range(8)] for c in "abc"]
    pairs = [(x, y, 1) for grp in groups for i, x in enumerate(grp) for y in grp[i + 1 :]]
    members = [n for grp in groups for n in grp]
    pairs += [(h, n, 10) for h in ("hub1", "hub2") for n in members]
    pairs += [("hub1", "hub2", 5)]
    return [{"entity_a": x, "entity_b": y, "w": w} for x, y, w in pairs]


@pytest.mark.asyncio
async def test_hub_fraction_escalates_until_the_giant_community_parts() -> None:
    ch = _CH(opted_in=[{"org": "o", "namespace": "n"}], walked=[], edges={"n": _fused_graph()})
    # 26 entities: 0.02 floors to no hub (fused), 0.04 → one hub (the other
    # still fuses the graph), 0.08 → both hubs: three communities of eight
    res = await run_entity_communities(
        EntityCommunitiesConfig(hub_drop_fraction=0.02, hub_drop_max_fraction=0.2), ch=ch
    )
    assert res.hub_fraction_used == {"o/n": 0.08}
    assert res.giant == []
    assert res.per_namespace["o/n"] == 3 and res.rows_written == 24
    _t, rows = ch.inserted[0]
    by_entity = {r["entity"]: r["community_id"] for r in rows}
    assert by_entity["a0"] == by_entity["a7"] != by_entity["b0"] == by_entity["b7"]
    assert "hub1" not in by_entity and "hub2" not in by_entity
    assert set(res.hubs_dropped["o/n"]) == {"hub1", "hub2"}


@pytest.mark.asyncio
async def test_escalation_stops_at_the_ceiling_and_records_the_giant() -> None:
    ch = _CH(opted_in=[{"org": "o", "namespace": "n"}], walked=[], edges={"n": _fused_graph()})
    res = await run_entity_communities(
        EntityCommunitiesConfig(hub_drop_fraction=0.02, hub_drop_max_fraction=0.04), ch=ch
    )
    # 0.02 → 0.04 (one hub) and no further: one community of the other 25
    assert res.hub_fraction_used == {"o/n": 0.04}
    assert res.giant == ["o/n"]
    assert res.per_namespace["o/n"] == 1 and res.rows_written == 25


@pytest.mark.asyncio
async def test_a_partition_that_parts_at_the_default_fraction_is_untouched() -> None:
    groups = [[f"{c}{i}" for i in range(4)] for c in "abc"]
    pairs = [(x, y, 1) for grp in groups for i, x in enumerate(grp) for y in grp[i + 1 :]]
    edges = [{"entity_a": x, "entity_b": y, "w": w} for x, y, w in pairs]
    ch = _CH(opted_in=[{"org": "o", "namespace": "n"}], walked=[], edges={"n": edges})
    res = await run_entity_communities(EntityCommunitiesConfig(hub_drop_fraction=0.002), ch=ch)
    assert res.hub_fraction_used == {} and res.giant == [] and res.hubs_dropped == {}
    assert res.per_namespace["o/n"] == 3
    # giant_share 0 never escalates: the fused graph is written as one community
    ch = _CH(opted_in=[{"org": "o", "namespace": "n"}], walked=[], edges={"n": _fused_graph()})
    res = await run_entity_communities(
        EntityCommunitiesConfig(hub_drop_fraction=0.02, giant_share=0.0), ch=ch
    )
    assert res.hub_fraction_used == {} and res.giant == [] and res.per_namespace["o/n"] == 1


@pytest.mark.asyncio
async def test_a_graph_too_small_for_a_hub_is_one_community_and_nothing_is_reported() -> None:
    # 14 entities in one cluster: 0.032 floors to no hub at every step, so
    # the cluster is its own partition — not escalated, not giant
    names = [f"n{i}" for i in range(14)]
    pairs = [(a, b, 1) for i, a in enumerate(names) for b in names[i + 1 : i + 4]]
    edges = [{"entity_a": x, "entity_b": y, "w": w} for x, y, w in pairs]
    ch = _CH(opted_in=[{"org": "o", "namespace": "n"}], walked=[], edges={"n": edges})
    res = await run_entity_communities(EntityCommunitiesConfig(), ch=ch)
    assert res.per_namespace["o/n"] == 1 and res.rows_written == 14
    assert res.hub_fraction_used == {} and res.giant == [] and res.hubs_dropped == {}


@pytest.mark.asyncio
async def test_escalation_stops_when_a_step_drops_no_further_hub() -> None:
    # 26 entities, ceiling 0.05: 0.02 → 0, 0.04 → 1 hub, 0.05 → floor(1.3) = 1
    # hub again — the same partition, so the pass stops at 0.04 and reports it
    ch = _CH(opted_in=[{"org": "o", "namespace": "n"}], walked=[], edges={"n": _fused_graph()})
    res = await run_entity_communities(
        EntityCommunitiesConfig(hub_drop_fraction=0.02, hub_drop_max_fraction=0.05), ch=ch
    )
    assert res.hub_fraction_used == {"o/n": 0.04} and res.giant == ["o/n"]
    assert res.hubs_dropped["o/n"] == ["hub1"] or res.hubs_dropped["o/n"] == ["hub2"]


def test_largest_share() -> None:
    assert largest_share([]) == 0.0
    edges = [("a", "b", 1.0), ("b", "c", 1.0), ("x", "y", 1.0)]
    comms = summarize_communities(label_propagation(edges), {})
    assert largest_share(comms) == 3 / 5


def test_worker_reads_the_walked_community_settings() -> None:
    from server.settings import settings

    assert settings.entity_communities_walked_days == 14
    assert settings.entity_communities_giant_share == 0.5
    assert settings.entity_communities_hub_drop_max_fraction == 0.032
