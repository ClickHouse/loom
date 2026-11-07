from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _fresh_walk_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """A walk is remembered for a minute; each test
    walks afresh. The graph-write marks read as no write and nothing in flight."""
    from server.memory.app import facts as _facts
    from server.memory.app import graph_write_marks

    _facts.reset_traverse_cache()

    async def _never_written(org: str, ns: str, *, wait_s: float) -> tuple[None, datetime]:
        return None, datetime.now(UTC)

    monkeypatch.setattr(graph_write_marks, "last_write", _never_written)


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        return []


@pytest.mark.asyncio
async def test_facts_list_defaults_to_current_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)

    await facts_handlers.handle_facts_list(
        FactsListInput(org="clickhouse", namespace="ns", entity="user")
    )

    sql, params = ch.query_all_calls[0]
    assert "(valid_to IS NULL OR valid_to >= now())" in sql
    assert "as_of" not in params


@pytest.mark.asyncio
async def test_facts_related_as_of_uses_interval_containment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsRelatedInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    as_of = datetime(2024, 6, 1, tzinfo=UTC)

    await facts_handlers.handle_facts_related(
        FactsRelatedInput(
            org="clickhouse",
            namespace="ns",
            entity="user",
            as_of=as_of,
        )
    )

    sql, params = ch.query_all_calls[0]
    assert "valid_from IS NULL OR valid_from <= {as_of:DateTime}" in sql
    assert "valid_to IS NULL OR valid_to >= {as_of:DateTime}" in sql
    assert params["as_of"] == as_of


@pytest.mark.asyncio
async def test_facts_traverse_defaults_edges_to_current_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)

    await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="clickhouse",
            namespace="ns",
            seed_entity="user",
        )
    )

    sql, params = ch.query_all_calls[0]
    assert "valid_to IS NULL OR valid_to >= now()" in sql
    assert "as_of" not in params


@pytest.mark.asyncio
async def test_facts_traverse_seeds_an_error_name_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write side stores "error:Timeouts" as the node "error:timeouts"; the walk
    starts there, not at a singular "error:timeout"."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="clickhouse", namespace="ns", seed_entity="error:Timeouts")
    )
    assert ch.query_all_calls[0][1]["frontier"] == ["error:timeouts"]
    assert out.seed == "error:timeouts"


@pytest.mark.asyncio
async def test_facts_traverse_aggregates_edge_pairs_before_min_co_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """entity_edges holds one co_occur=1 row per observation, so the
    edge set must sum per (src, dst) pair and filter the pair total in
    HAVING — a raw-row WHERE filter makes min_co_occur > 1 match
    nothing on production-written graphs, and duplicate rows multiply
    recursion branches and evict distinct neighbors from the top-K trim.
    """
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)

    await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="clickhouse",
            namespace="ns",
            seed_entity="user",
            min_co_occur=2,
        )
    )

    sql, params = ch.query_all_calls[0]
    # Aggregating subquery: both edge directions unioned, summed per pair.
    assert "toUInt32(sum(co_occur)) AS pair_co_occur" in sql
    assert "GROUP BY src, dst" in sql
    assert "HAVING pair_co_occur >= {min_co:UInt32}" in sql
    assert params["min_co"] == 2
    # The aggregated pair total is what the hop selects (it becomes via_co_occur).
    assert "SELECT src, dst, relation, direction, pair_co_occur, provenance" in sql
    assert "co_occur AS via_co_occur" not in sql
    # The trim is part of the hop, not an outer pass.
    assert "LIMIT {top_k_per_hop:UInt32} BY src" in sql
    assert "WITH RECURSIVE" not in sql
    # No raw-row co_occur filter anywhere (it would apply per insert row).
    assert "AND co_occur >= {min_co:UInt32}" not in sql
    # Both directions remain org/namespace scoped.
    assert sql.count("entity_a AS src, entity_b AS dst") == 1
    assert sql.count("entity_b AS src, entity_a AS dst") == 1
    assert sql.count("org = {org:String}") >= 2


@pytest.mark.asyncio
async def test_facts_traverse_aggregation_preserves_as_of_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The validity predicate must filter raw edge rows BEFORE the sum,
    i.e. inside both UNION ALL legs of the aggregating subquery."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    as_of = datetime(2024, 6, 1, tzinfo=UTC)

    await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="clickhouse",
            namespace="ns",
            seed_entity="user",
            as_of=as_of,
        )
    )

    sql, params = ch.query_all_calls[0]
    assert params["as_of"] == as_of
    inner = sql.split("GROUP BY src, dst")[0]
    assert inner.count("valid_from IS NULL OR valid_from <= {as_of:DateTime}") == 2
    assert inner.count("valid_to IS NULL OR valid_to >= {as_of:DateTime}") == 2


@pytest.mark.asyncio
async def test_facts_list_min_co_occur_filters_aggregate_via_having(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`co_occur` in facts.list's SELECT is the read-time sum() alias;
    filtering it in WHERE is rejected by ClickHouse with
    ILLEGAL_AGGREGATION, so min_co_occur must land in HAVING — and the
    HAVING must use the alias (repeating sum(co_occur) nests the
    aggregate after alias substitution, also ILLEGAL_AGGREGATION)."""
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)

    await facts_handlers.handle_facts_list(
        FactsListInput(org="clickhouse", namespace="ns", entity="user", min_co_occur=2)
    )

    sql, params = ch.query_all_calls[0]
    assert "HAVING co_occur >= {min_co:UInt32}" in sql
    assert "HAVING sum(co_occur)" not in sql
    assert params["min_co"] == 2
    where_part = sql.split("GROUP BY")[0]
    assert "min_co" not in where_part
    # HAVING must follow the GROUP BY it filters.
    assert sql.index("GROUP BY subject, relation, object") < sql.index("HAVING")


@pytest.mark.asyncio
async def test_facts_list_default_min_co_occur_adds_no_having(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)

    await facts_handlers.handle_facts_list(
        FactsListInput(org="clickhouse", namespace="ns", entity="user")
    )

    sql, params = ch.query_all_calls[0]
    assert "HAVING" not in sql
    assert "min_co" not in params


@pytest.mark.asyncio
async def test_facts_traverse_typed_walk_uses_triples_with_relation_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`relations` or `labeled` switches the edge set to loom.entity_triples,
    both directions, grouped by relation so a hop can report its label."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="clickhouse",
            namespace="ns",
            seed_entity="MergeTreeData",
            relations=["Modifies", "wait on by"],
        )
    )
    sql, params = ch.query_all_calls[0]
    assert "FROM loom.entity_triples" in sql and "FROM loom.entity_edges" not in sql
    assert (
        "SELECT subject AS src, object AS dst" in sql
        and "SELECT object AS src, subject AS dst" in sql
    )
    assert "'out' AS direction" in sql and "'in' AS direction" in sql
    assert "GROUP BY src, dst, relation, direction" in sql
    # labels canonicalised exactly as the write path stores them (facts.path does the same)
    assert (
        params["rels"]
        == [
            facts_handlers._query_relation("Modifies"),
            facts_handlers._query_relation("wait on by"),
        ]
        == ["modifi", "wait_on_by"]
    )
    assert "relation, 'out' AS direction" in sql and "relation, 'in' AS direction" in sql


@pytest.mark.asyncio
async def test_facts_traverse_default_walk_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="clickhouse", namespace="ns", seed_entity="user")
    )
    sql, params = ch.query_all_calls[0]
    assert "FROM loom.entity_edges" in sql and "entity_triples" not in sql
    assert "GROUP BY src, dst, relation, direction" in sql, "same aggregation shape, empty labels"
    assert "rels" not in params


@pytest.mark.asyncio
async def test_facts_traverse_colours_nodes_by_community(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every returned node carries its community from the latest
    entity_communities pass; unclustered entities read None."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    class _CH(StubCH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.query_all_calls.append((sql, params or {}))
            if "FROM loom.entity_communities" in sql:
                return [
                    {"entity": "user", "community_id": 0},
                    {"entity": "alice", "community_id": 1},
                ]
            if params and params.get("frontier") == ["user"]:
                return [
                    {
                        "src": "user",
                        "dst": "alice",
                        "relation": "",
                        "direction": "",
                        "pair_co_occur": 3,
                        "provenance": "",
                    },
                    {
                        "src": "user",
                        "dst": "zed",
                        "relation": "",
                        "direction": "",
                        "pair_co_occur": 1,
                        "provenance": "",
                    },
                ]
            return []

    ch = _CH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="clickhouse", namespace="ns", seed_entity="User")
    )
    assert out.seed_community_id == 0
    assert {n.entity: n.community_id for n in out.nodes} == {"alice": 1, "zed": None}
    lookup = [p for q, p in ch.query_all_calls if "FROM loom.entity_communities" in q][0]
    assert set(lookup["ents"]) == {"user", "alice", "zed"} and lookup["nss"] == ["ns"]


@pytest.mark.asyncio
async def test_traverse_seed_is_canonicalised_like_the_write_path(monkeypatch) -> None:
    """ "Alice's" must reach the rows written for "alice"; a bare lower() did
    not (facts.path already canonicalises both ends)."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    class _CH:
        def __init__(self) -> None:
            self.query_all_calls: list[tuple[str, dict]] = []

        async def query_all(self, sql: str, params: dict | None = None):
            self.query_all_calls.append((sql, params or {}))
            return []

    ch = _CH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    for raw, want in (
        ("Alice's", "alice"),
        ("The user", "user"),
        ("I", "user"),
        ("MergeTreeData", "mergetreedata"),
    ):
        ch.query_all_calls.clear()
        out = await facts_handlers.handle_facts_traverse(
            FactsTraverseInput(org="clickhouse", namespace="ns", seed_entity=raw)
        )
        assert out.seed == want
        assert ch.query_all_calls and ch.query_all_calls[0][1]["frontier"] == [want]


@pytest.mark.asyncio
async def test_facts_list_source_fanout_keeps_final_and_maps_keys_onto_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This read keeps FINAL: on the all-key link table the streaming merge is
    cheaper in wall time than a GROUP BY (+1-3 ms, and 25/200 capped picks
    changed, for -1.6..-2.1x bytes). The shape is pinned so a byte-only
    rewrite re-measures first. Rows with keys reach `source_keys`."""
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app import facts as facts_handlers

    class _CH(StubCH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.query_all_calls.append((sql, params or {}))
            if "FROM loom.entity_triple_memories" in sql:
                return [
                    {
                        "subject": "alice",
                        "relation": "works_at",
                        "object": "acme",
                        "source_keys": ["k1", "k2"],
                    },
                ]
            return [
                {
                    "subject": "alice",
                    "relation": "works_at",
                    "object": "acme",
                    "co_occur": 3,
                    "last_seen": datetime(2026, 9, 1),
                    "provenance": "extracted",
                },
                {
                    "subject": "bob",
                    "relation": "likes",
                    "object": "tea",
                    "co_occur": 1,
                    "last_seen": datetime(2026, 9, 2),
                    "provenance": "",
                },
            ]

    ch = _CH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_list(
        FactsListInput(org="clickhouse", namespace="ns", include_sources=True)
    )
    assert [(r.subject, r.source_keys) for r in out.rows] == [("alice", ["k1", "k2"]), ("bob", [])]
    assert len(ch.query_all_calls) == 2
    sql, params = ch.query_all_calls[1]
    assert "FROM loom.entity_triple_memories FINAL" in sql
    assert "groupArray(10)(memory_key) AS source_keys" in sql
    assert "GROUP BY subject, relation, object" in sql and "PREWHERE" not in sql
    assert sorted(params["subjects"]) == ["alice", "bob"]
    assert sorted(params["relations"]) == ["likes", "works_at"]
    assert sorted(params["objects"]) == ["acme", "tea"]


def _edge(src: str, dst: str, w: int) -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "relation": "",
        "direction": "",
        "pair_co_occur": w,
        "provenance": "",
    }


class _HopCH(StubCH):
    """Scripted per-frontier answers: `edges[entity]` lists (dst, weight)."""

    def __init__(self, edges: dict[str, list[tuple[str, int]]]) -> None:
        super().__init__()
        self.edges = edges

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        if "FROM loom.entity_communities" in sql:
            return []
        p = params or {}
        out: list[dict[str, Any]] = []
        for src in p.get("frontier", []):
            cands = [(d, w) for d, w in self.edges.get(src, []) if d not in p.get("visited", [])]
            cands.sort(key=lambda x: (-x[1], x[0]))
            out.extend(_edge(src, d, w) for d, w in cands[: int(p.get("top_k_per_hop", 20))])
        return out


@pytest.mark.asyncio
async def test_traverse_trims_each_hop_before_expanding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hop 2 expands only the top-K hop-1 nodes, never every
    neighbour; one query per hop, the frontier of hop h+1 is exactly what
    hop h kept."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _HopCH(
        {
            "hub": [("a", 9), ("b", 8), ("c", 7), ("d", 1)],
            "a": [("a1", 5), ("hub", 9)],
            "b": [("b1", 4), ("a", 2)],
            "c": [("c1", 3)],
            "d": [("d1", 100)],
        }
    )
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity="hub", max_hops=2, top_k_per_hop=3)
    )
    walks = [p for q, p in ch.query_all_calls if "frontier" in p]
    assert walks[0]["frontier"] == ["hub"] and walks[0]["visited"] == ["hub"]
    assert walks[1]["frontier"] == ["a", "b", "c"], "d was trimmed at hop 1 and never expands"
    assert set(walks[1]["visited"]) == {"hub", "a", "b", "c"}
    got = {n.entity: (n.hop, n.via_co_occur, n.path) for n in out.nodes}
    assert set(got) == {"a", "b", "c", "a1", "b1", "c1"}, "d1 (behind the trimmed d) is unreachable"
    assert got["a1"] == (2, 5, ["hub", "a", "a1"])
    # `a` reached at hop 1 is not reported again from `b` at hop 2.
    assert got["a"][0] == 1
    assert [n.hop for n in out.nodes] == sorted(n.hop for n in out.nodes)
    assert out.truncated is False


@pytest.mark.asyncio
async def test_traverse_max_nodes_caps_the_walk_and_flags_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _HopCH({"s": [(f"n{i}", 10 - i) for i in range(8)], "n0": [("deep", 1)]})
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o", namespace="n", seed_entity="s", max_hops=3, top_k_per_hop=20, max_nodes=5
        )
    )
    assert [n.entity for n in out.nodes] == ["n0", "n1", "n2", "n3", "n4"], (
        "strongest pairs survive the cap"
    )
    assert out.truncated is True
    assert len([p for q, p in ch.query_all_calls if "frontier" in p]) == 1, (
        "a capped hop ends the walk"
    )


@pytest.mark.asyncio
async def test_traverse_seed_falls_back_to_the_spelling_the_edges_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace can write "prs" into entity_edges while the taxonomy
    lists "pr": the walk tries the canonical seed, then cheap variants."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _HopCH({"prs": [("review", 3)]})
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity="pr", max_hops=1)
    )
    assert out.seed == "prs"
    assert [n.entity for n in out.nodes] == ["review"]
    frontiers = [p["frontier"] for q, p in ch.query_all_calls if "frontier" in p]
    assert frontiers[0] == ["pr"] and frontiers[1] == ["prs"]


@pytest.mark.asyncio
async def test_traverse_unknown_seed_returns_no_nodes_after_the_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _HopCH({})
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity="ghost", max_hops=3)
    )
    assert out.nodes == [] and out.seed == "ghost" and out.truncated is False
    assert len(ch.query_all_calls) <= 3, "one probe per spelling variant, no further hops"


@pytest.mark.asyncio
async def test_facts_list_source_status_flags_a_triple_with_no_active_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planes disagree when an open triple's every source row is
    superseded (a common multi-hop miss); `include_source_status` reads the
    rows' statuses in one more
    round-trip and flags the triple. A key with several rows reads live when
    any is; no known status is not evidence."""
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app import facts as facts_handlers

    class _CH(StubCH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.query_all_calls.append((sql, params or {}))
            if "FROM loom.entity_triple_memories" in sql:
                return [
                    {
                        "subject": "fatah",
                        "relation": "chairperson_of",
                        "object": "mahmoud abba",
                        "source_keys": ["fatah_chairperson_of"],
                    },
                    {
                        "subject": "fatah",
                        "relation": "chairperson_of",
                        "object": "moshe kahlon",
                        "source_keys": ["moshe_kahlon_chairperson", "fatah_chairperson"],
                    },
                ]
            if "FROM loom.memory_embeddings FINAL" in sql:
                assert sorted(params["keys"]) == [
                    "fatah_chairperson",
                    "fatah_chairperson_of",
                    "moshe_kahlon_chairperson",
                ]
                return [
                    {"memory_key": "fatah_chairperson_of", "statuses": ["superseded"]},
                    {
                        "memory_key": "moshe_kahlon_chairperson",
                        "statuses": ["superseded", "active"],
                    },
                ]
            return [
                {
                    "subject": "fatah",
                    "relation": "chairperson_of",
                    "object": "mahmoud abba",
                    "co_occur": 2,
                    "last_seen": datetime(2026, 9, 14),
                    "provenance": "extracted",
                },
                {
                    "subject": "fatah",
                    "relation": "chairperson_of",
                    "object": "moshe kahlon",
                    "co_occur": 1,
                    "last_seen": datetime(2026, 9, 14),
                    "provenance": "extracted",
                },
                {
                    "subject": "fatah",
                    "relation": "found_in",
                    "object": "1959",
                    "co_occur": 1,
                    "last_seen": datetime(2026, 9, 14),
                    "provenance": "",
                },
            ]

    ch = _CH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_list(
        FactsListInput(org="clickhouse", namespace="ns", entity="fatah", include_source_status=True)
    )
    by_obj = {r.object: r for r in out.rows}
    assert by_obj["mahmoud abba"].stale is True
    assert [(s.memory_key, s.status) for s in by_obj["mahmoud abba"].sources] == [
        ("fatah_chairperson_of", "superseded")
    ]
    assert by_obj["moshe kahlon"].stale is False
    assert {s.status for s in by_obj["moshe kahlon"].sources} == {"active", ""}
    assert by_obj["1959"].stale is False and by_obj["1959"].sources == []
    # include_sources was not asked for: the keys stay off the wire, the statuses ride
    assert by_obj["mahmoud abba"].source_keys == []
    assert len(ch.query_all_calls) == 3
    assert facts_handlers.source_status(["superseded", "duplicate"]) == "superseded"
    assert (
        facts_handlers.triple_is_stale([]) is False
        and facts_handlers.triple_is_stale(["", ""]) is False
    )


class _HubHopCH(_HopCH):
    """_HopCH plus a scripted degree table: `degrees[entity]` answers the
    hub lookup (entities at or above the threshold)."""

    def __init__(self, edges: dict[str, list[tuple[str, int]]], degrees: dict[str, int]) -> None:
        super().__init__(edges)
        self.degrees = degrees

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        # The degree READ is the statement without the hop's join; a ranked
        # hop also names entity_degree but carries `frontier`.
        if "FROM loom.entity_degree" in sql and "frontier" not in (params or {}):
            self.query_all_calls.append((sql, params or {}))
            p = params or {}
            return [
                {"entity": e, "d": self.degrees.get(e, 0)}
                for e in p.get("ents", [])
                if e in self.degrees
            ]
        return await super().query_all(sql, params)


_HUB_GRAPH = {
    "alice": [("user", 50), ("acme", 9), ("bob", 4)],
    "user": [("weather", 40), ("lunch", 30), ("alice", 50)],
    "acme": [("q3_plan", 6), ("user", 20)],
    "bob": [("tea", 2)],
}
_HUB_DEGREES = {"user": 4_505, "alice": 60, "acme": 40, "bob": 8}


@pytest.mark.asyncio
async def test_traverse_hub_policy_reports_a_hub_but_does_not_expand_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`user` (degree 4,505) is reached at hop 1 and reported
    with `hub=True`, but its neighbourhood (weather, lunch) never enters the
    walk; the other hop-1 nodes expand as before. One degree read per hop,
    over that hop's new nodes only."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o",
            namespace="n",
            seed_entity="alice",
            max_hops=2,
            top_k_per_hop=5,
            hub_degree=500,
        )
    )
    by_name = {n.entity: n for n in out.nodes}
    assert set(by_name) == {"user", "acme", "bob", "q3_plan", "tea"}
    assert by_name["user"].hub is True and by_name["user"].hop == 1
    assert all(not n.hub for n in out.nodes if n.entity != "user")
    assert out.hubs == ["user"]
    assert out.truncated is False, "a held hub is a choice, not a truncation"
    walks = [p for q, p in ch.query_all_calls if "frontier" in p]
    assert walks[1]["frontier"] == ["acme", "bob"], "the hub left the frontier"
    degree_reads = [
        (q, p) for q, p in ch.query_all_calls if "FROM loom.entity_degree" in q and "ents" in p
    ]
    assert len(degree_reads) == 2, "one read per hop"
    assert sorted(degree_reads[0][1]["ents"]) == ["acme", "bob", "user"]
    assert degree_reads[0][1]["ns"] == "n" and "hub" not in degree_reads[0][1], (
        "the rule is applied in Python, the read returns every degree"
    )
    assert "sum(degree) AS d" in degree_reads[0][0] and "HAVING" not in degree_reads[0][0]
    assert by_name["user"].degree == 4_505 and by_name["acme"].degree == 40, (
        "every reached node carries the degree the policy read"
    )


@pytest.mark.asyncio
async def test_traverse_hub_policy_never_holds_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeding from the hub itself is how a caller asks for its neighbourhood:
    the seed's degree is never consulted and the walk expands it."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o", namespace="n", seed_entity="user", max_hops=1, top_k_per_hop=5, hub_degree=500
        )
    )
    assert {n.entity for n in out.nodes} == {"weather", "lunch", "alice"}
    assert out.hubs == []
    degree_reads = [p for q, p in ch.query_all_calls if "ents" in p]
    assert degree_reads and "user" not in degree_reads[0]["ents"]


@pytest.mark.asyncio
async def test_traverse_hub_policy_is_off_by_default_and_reads_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off: the walk is byte-for-byte the plain one — no
    degree read, the hub expands, `hubs` is empty. The request's
    `hub_degree` turns the absolute rule on."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers
    from server.settings import settings

    monkeypatch.setattr(settings, "facts_traverse_hub_share", 0.0, raising=False)
    ch = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity="alice", max_hops=2, top_k_per_hop=5)
    )
    assert "weather" in {n.entity for n in out.nodes}, "off = the hub expands"
    assert out.hubs == [] and all(not n.hub for n in out.nodes)
    assert not [q for q, p in ch.query_all_calls if "ents" in p and "entity_degree" in q]

    facts_handlers.reset_traverse_cache()
    ch2 = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch2)
    out2 = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o", namespace="n", seed_entity="alice", max_hops=2, top_k_per_hop=5, hub_degree=500
        )
    )
    assert out2.hubs == ["user"] and "weather" not in {n.entity for n in out2.nodes}


@pytest.mark.asyncio
async def test_traverse_hub_lookup_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A namespace whose degree table cannot be read walks as if the policy
    were off; the graph step never blocks an answer."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    class _Broken(_HopCH):
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            if "FROM loom.entity_degree" in sql and "frontier" not in (params or {}):
                raise RuntimeError("Table loom.entity_degree does not exist")
            return await super().query_all(sql, params)

    ch = _Broken(_HUB_GRAPH)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o",
            namespace="n",
            seed_entity="alice",
            max_hops=2,
            top_k_per_hop=5,
            hub_degree=500,
        )
    )
    assert "weather" in {n.entity for n in out.nodes} and out.hubs == []


def test_is_hub_rules_compose_and_ignore_unknown_degrees() -> None:
    """A node is a hub by the absolute rule (degree >=
    hub_degree) or the relative one (via_co_occur / degree <= hub_share);
    a node the degree table does not know is never a hub; 0 turns a rule off."""
    from server.memory.app.facts import is_hub

    # clickhouse -> bug on a large graph: 191 of 56,321 (0.34%).
    assert is_hub(191, 56_321, hub_degree=0, hub_share=0.01) is True
    # alice -> acme: 9 of 40 (22%) — a strong, specific pair, even though
    # acme's degree is above a low absolute threshold.
    assert is_hub(9, 40, hub_degree=0, hub_share=0.01) is False
    assert is_hub(9, 40, hub_degree=30, hub_share=0.0) is True, (
        "the absolute rule alone still holds it"
    )
    assert is_hub(9, 40, hub_degree=0, hub_share=0.0) is False, "both off = expand"
    assert is_hub(1, 0, hub_degree=1, hub_share=0.5) is False, "unknown degree is never a hub"
    assert is_hub(2, 100, hub_degree=0, hub_share=0.02) is True, "at the share counts as held"


@pytest.mark.asyncio
async def test_traverse_hub_share_holds_the_node_the_edge_barely_explains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the unit graph: alice -> user is 50 of 4,505 (1.1%),
    alice -> acme 9 of 40 (22%), alice -> bob 4 of 8 (50%). At hub_share 0.02
    only `user` is held; the absolute rule at 500 would have held the same
    node here, but on a graph where every strong pair is a hub the share
    rule keeps expanding the specific ones."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o",
            namespace="n",
            seed_entity="alice",
            max_hops=2,
            top_k_per_hop=5,
            hub_degree=0,
            hub_share=0.02,
        )
    )
    by_name = {n.entity: n for n in out.nodes}
    assert out.hubs == ["user"] and by_name["user"].hub is True
    assert {n.entity for n in out.nodes} == {"user", "acme", "bob", "q3_plan", "tea"}
    walks = [p for q, p in ch.query_all_calls if "frontier" in p]
    assert walks[1]["frontier"] == ["acme", "bob"]
    # A looser share holds acme too (9 / 40 = 0.225 <= 0.25) and bob expands alone.
    facts_handlers.reset_traverse_cache()
    ch2 = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch2)
    out2 = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o",
            namespace="n",
            seed_entity="alice",
            max_hops=2,
            top_k_per_hop=5,
            hub_degree=0,
            hub_share=0.25,
        )
    )
    assert out2.hubs == ["user", "acme"]
    assert {n.entity for n in out2.nodes} == {"user", "acme", "bob", "tea"}


@pytest.mark.asyncio
async def test_traverse_hub_share_reads_the_setting_and_a_zero_request_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers
    from server.settings import settings

    monkeypatch.setattr(settings, "facts_traverse_hub_share", 0.02, raising=False)
    ch = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity="alice", max_hops=2, top_k_per_hop=5)
    )
    assert out.hubs == ["user"], "the deployment default applies when the request is silent"

    facts_handlers.reset_traverse_cache()
    ch2 = _HubHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch2)
    out2 = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o", namespace="n", seed_entity="alice", max_hops=2, top_k_per_hop=5, hub_share=0
        )
    )
    assert out2.hubs == [] and "weather" in {n.entity for n in out2.nodes}
    assert not [q for q, p in ch2.query_all_calls if "ents" in p and "entity_degree" in q], (
        "both rules off = no degree read at all"
    )


def test_hop_sql_rank_by_co_occur_is_the_plain_statement_and_specificity_joins_degrees() -> None:
    """`co_occur` leaves the hop statement as it was (no join,
    ordered by co-mentions); `specificity` / `cosine` join entity_degree on
    the neighbour, order by the ratio with co-mentions as the tie-break, and
    return `degree` so the hub policy needs no second read."""
    from server.memory.store.fact_graph import _traverse_hop_sql

    base = _traverse_hop_sql(typed=False, relation_predicate="", validity_predicate="")
    same = _traverse_hop_sql(
        typed=False, relation_predicate="", validity_predicate="", rank_by="co_occur"
    )
    assert base == same and "JOIN" not in base and "ORDER BY src, pair_co_occur DESC, dst" in base
    spec = _traverse_hop_sql(
        typed=False, relation_predicate="", validity_predicate="", rank_by="specificity"
    )
    assert "LEFT JOIN" in spec and "FROM loom.entity_degree" in spec
    assert "toFloat64(p.pair_co_occur) / greatest(degree, pair_co_occur) AS rank_score" in spec
    assert "ORDER BY src, rank_score DESC, pair_co_occur DESC, dst" in spec
    assert spec.strip().startswith(
        "SELECT src, dst, relation, direction, pair_co_occur, provenance, degree"
    )
    assert "LIMIT {top_k_per_hop:UInt32} BY src" in spec
    cos = _traverse_hop_sql(
        typed=True, relation_predicate="", validity_predicate="", rank_by="cosine"
    )
    assert "/ sqrt(greatest(degree, pair_co_occur)) AS rank_score" in cos
    assert "FROM loom.entity_triples" in cos, "the typed edge set gets the same join"


class _RankedHopCH(_HubHopCH):
    """Plays the specificity-ranked hop: the same edges, ordered by
    pair / degree within each parent, carrying `degree` on every row."""

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        if "FROM loom.entity_degree" in sql and "frontier" not in (params or {}):
            return await super().query_all(sql, params)
        self.query_all_calls.append((sql, params or {}))
        p = params or {}
        out: list[dict[str, Any]] = []
        for src in p.get("frontier", []):
            cands = [(d, w) for d, w in self.edges.get(src, []) if d not in p.get("visited", [])]
            if "rank_score" in sql:
                cands.sort(
                    key=lambda x: (-(x[1] / max(self.degrees.get(x[0], 0), x[1])), -x[1], x[0])
                )
            else:
                cands.sort(key=lambda x: (-x[1], x[0]))
            for d, w in cands[: int(p.get("top_k_per_hop", 20))]:
                row = _edge(src, d, w)
                if "rank_score" in sql:
                    row["degree"] = self.degrees.get(d, 0)
                out.append(row)
        return out


@pytest.mark.asyncio
async def test_traverse_rank_by_specificity_keeps_the_pairs_the_parent_explains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the unit graph, alice's pairs by co-mentions are user 50 > acme 9 >
    bob 4; by specificity they are bob 4/8 > acme 9/40 > user 50/4,505. With
    K=2, `co_occur` keeps user and acme, `specificity` keeps bob and acme —
    and the hub policy reads no degrees of its own because the hop carried
    them."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers

    ch = _RankedHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    raw = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o",
            namespace="n",
            seed_entity="alice",
            max_hops=1,
            top_k_per_hop=2,
            hub_share=0.01,
            rank_by="co_occur",
        )
    )
    assert {n.entity for n in raw.nodes} == {"user", "acme"}
    facts_handlers.reset_traverse_cache()
    ch2 = _RankedHopCH(_HUB_GRAPH, _HUB_DEGREES)
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch2)
    spec = await facts_handlers.handle_facts_traverse(
        FactsTraverseInput(
            org="o",
            namespace="n",
            seed_entity="alice",
            max_hops=1,
            top_k_per_hop=2,
            hub_share=0.01,
            rank_by="specificity",
        )
    )
    assert {n.entity for n in spec.nodes} == {"bob", "acme"}
    assert {n.entity: n.degree for n in spec.nodes} == {"bob": 8, "acme": 40}, (
        "degree comes from the hop's join"
    )
    assert not [q for q, p in ch2.query_all_calls if "ents" in p and "entity_degree" in q], (
        "no separate degree read when the hop carried the degrees"
    )
    assert spec.hubs == [] and all(not n.hub for n in spec.nodes)


@pytest.mark.asyncio
async def test_traverse_rank_by_reads_the_setting_and_the_request_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import facts as facts_handlers
    from server.settings import settings

    with pytest.raises(ValidationError):
        FactsTraverseInput(org="o", namespace="n", seed_entity="alice", rank_by="pagerank")
    monkeypatch.setattr(settings, "facts_traverse_rank_by", "specificity", raising=False)
    assert (
        facts_handlers._rank_by_for(FactsTraverseInput(org="o", namespace="n", seed_entity="a"))
        == "specificity"
    )
    assert (
        facts_handlers._rank_by_for(
            FactsTraverseInput(org="o", namespace="n", seed_entity="a", rank_by="cosine")
        )
        == "cosine"
    )


@pytest.mark.asyncio
async def test_facts_list_and_related_match_the_canonical_and_the_lowered_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A facet row stores the canonical node; one from build N-1 holds the name lowered.
    from server.memory.api.models_facts import FactsListInput, FactsRelatedInput
    from server.memory.app import facts as facts_handlers
    from server.memory.domain.graph_quality import facet_triples

    ch = StubCH()
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    name = "error:HTTPError(404)"
    [(_, _, node)] = facet_triples("bot", [], [], [name], [])
    both = [node, name.lower()]

    await facts_handlers.handle_facts_list(FactsListInput(org="o", namespace="n", object=name))
    await facts_handlers.handle_facts_list(FactsListInput(org="o", namespace="n", entity=name))
    await facts_handlers.handle_facts_related(
        FactsRelatedInput(org="o", namespace="n", entity=name)
    )
    assert [p.get("obj") or p.get("ent") for _, p in ch.query_all_calls] == [both, both, both]
    assert "object IN {obj:Array(String)}" in ch.query_all_calls[0][0]
