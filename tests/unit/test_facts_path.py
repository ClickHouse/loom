"""facts.path — a path between two entities.

Pure search mechanics in memory/domain/graph_path plus the handler driving a
stubbed ClickHouse one frontier at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.domain import graph_path as gp


def _row(src: str, dst: str, co: int = 1, rel: str = "", direction: str = "", degree: int = 1):
    return {
        "src": src,
        "dst": dst,
        "relation": rel,
        "direction": direction,
        "pair_co_occur": co,
        "valid_from": None,
        "valid_to": None,
        "degree": degree,
    }


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_absorb_keeps_first_parent_and_advances_frontier() -> None:
    a = gp.Side(root="alice")
    new = gp.absorb(a, [_row("alice", "acme", 3), _row("alice", "bob", 1), _row("zed", "x")])
    assert new == ["acme", "bob"]  # "zed" is not on this side's frontier: ignored
    assert a.depth == 1 and a.frontier == ["acme", "bob"]
    # A later row for an already-reached entity does not overwrite the parent.
    gp.absorb(a, [_row("acme", "bob", 9), _row("acme", "retry storm")])
    assert a.parents["bob"][0] == "alice"
    assert a.depth_of("retry storm") == 2


def test_meet_picks_fewest_total_hops_and_orients_edges_a_to_b() -> None:
    a = gp.Side(root="alice")
    gp.absorb(a, [_row("alice", "acme", 3), _row("alice", "team", 1)])
    b = gp.Side(root="retry storm")
    gp.absorb(b, [_row("retry storm", "acme", 2, "caused", "out"), _row("retry storm", "pager")])
    hit = gp.meet(a, b, ["acme", "pager"])
    assert hit is not None
    nodes, edges = hit
    assert nodes == ["alice", "acme", "retry storm"]
    assert [(e.entity_a, e.entity_b) for e in edges] == [("alice", "acme"), ("acme", "retry storm")]
    # The b-side edge was fetched as retry storm -> acme ("out"); on the path
    # it reads acme -> retry storm, so its direction flips.
    assert edges[1].relation == "caused" and edges[1].direction == "in"
    assert edges[0].co_occur == 3 and edges[1].co_occur == 2


def test_meet_prefers_shorter_when_two_candidates() -> None:
    a = gp.Side(root="a")
    gp.absorb(a, [_row("a", "m1"), _row("a", "x")])
    gp.absorb(a, [_row("x", "m2")])
    b = gp.Side(root="b")
    gp.absorb(b, [_row("b", "m1"), _row("b", "m2")])
    nodes, _ = gp.meet(a, b, ["m2", "m1"])  # m1: 1+1, m2: 2+1
    assert nodes == ["a", "m1", "b"]
    assert gp.meet(a, b, ["nowhere"]) is None


def test_pick_side_expands_the_smaller_frontier() -> None:
    a = gp.Side(root="a", frontier=["a1", "a2", "a3"])
    b = gp.Side(root="b", frontier=["b1"])
    assert gp.pick_side(a, b) is b
    b.frontier = ["b1", "b2", "b3"]
    assert gp.pick_side(a, b) is a  # full tie goes to a
    a.depth = 1
    assert gp.pick_side(a, b) is b  # equal frontiers: the shallower side, so they alternate


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


class StubCH:
    def __init__(self, frontiers: list[list[dict[str, Any]]]) -> None:
        self.frontiers = list(frontiers)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return self.frontiers.pop(0) if self.frontiers else []


def _input(**kw: Any):
    from server.memory.api.models_facts import FactsPathInput

    base = {
        "org": "clickhouse",
        "namespace": "ns",
        "entity_a": "Alice's",
        "entity_b": "Retry Storms",
    }
    base.update(kw)
    return FactsPathInput(**base)


@pytest.mark.asyncio
async def test_path_found_meeting_in_the_middle(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import facts as facts_handlers

    ch = StubCH(
        [
            [_row("alice", "acme", 4), _row("alice", "bob", 1)],  # expand a (tie → a)
            [_row("retry storm", "acme", 2)],  # b's frontier is smaller now (1 < 2)
        ]
    )
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_path(_input())
    assert out.found is True
    assert out.entity_a == "alice" and out.entity_b == "retry storm"  # canonicalised
    assert out.path == ["alice", "acme", "retry storm"]
    assert out.hops == 2 and len(out.edges) == 2 and out.truncated is False
    assert [e.co_occur for e in out.edges] == [4, 2]
    # Each frontier query names the side being expanded and shares traverse's
    # default validity and aggregation.
    (sql0, p0), (sql1, p1) = ch.calls
    assert p0["frontier"] == ["alice"] and p1["frontier"] == ["retry storm"]
    assert "(valid_to IS NULL OR valid_to >= now())" in sql0 and "valid_as_of" not in p0
    assert "toUInt32(sum(co_occur)) AS pair_co_occur" in sql0
    assert "HAVING pair_co_occur >= {min_co:UInt32}" in sql0
    assert "loom.entity_edges" in sql0 and "loom.entity_triples" not in sql0


@pytest.mark.asyncio
async def test_same_entity_is_a_zero_hop_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import facts as facts_handlers

    ch = StubCH([])
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_path(_input(entity_a="The User", entity_b="user"))
    assert out.found and out.path == ["user"] and out.hops == 0 and out.edges == []
    assert ch.calls == []


@pytest.mark.asyncio
async def test_not_connected_is_not_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import facts as facts_handlers

    # a has one neighbour, b has none: b's frontier empties, the loop stops
    # before the budget, and nothing was trimmed → a definite "no".
    ch = StubCH([[_row("alice", "acme")], []])
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_path(_input(max_hops=4))
    assert out.found is False and out.path == [] and out.truncated is False
    assert out.hops == 2


@pytest.mark.asyncio
async def test_budget_exhausted_with_live_frontiers_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import facts as facts_handlers

    ch = StubCH([[_row("alice", "acme")], [_row("retry storm", "pager")]])
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_path(_input(max_hops=2))
    assert out.found is False and out.truncated is True and out.hops == 2
    assert len(ch.calls) == 2  # never a third query past the budget


@pytest.mark.asyncio
async def test_per_hop_trim_marks_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import facts as facts_handlers

    ch = StubCH([[_row("alice", "acme", degree=50)], []])
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    out = await facts_handlers.handle_facts_path(_input(top_k_per_hop=1, max_hops=3))
    assert out.found is False and out.truncated is True


@pytest.mark.asyncio
async def test_as_of_and_typed_relations_mirror_traverse(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import facts as facts_handlers

    ch = StubCH(
        [
            [_row("alice", "acme", 1, "work_at", "out")],
            [_row("retry storm", "acme", 1, "caus", "out")],
        ]
    )
    monkeypatch.setattr(facts_handlers, "get_client", lambda: ch)
    valid_as_of = datetime(2024, 6, 1, tzinfo=UTC)
    out = await facts_handlers.handle_facts_path(
        _input(valid_as_of=valid_as_of, relations=["works at", "Causes"])
    )
    sql, params = ch.calls[0]
    assert "loom.entity_triples" in sql and "loom.entity_edges" not in sql
    assert "valid_from IS NULL OR valid_from <= {valid_as_of:DateTime}" in sql
    assert "valid_to IS NULL OR valid_to >= {valid_as_of:DateTime}" in sql
    assert params["valid_as_of"] == valid_as_of
    assert params["rels"] == ["work_at", "caus"]  # normalised like the write path
    assert out.found
    assert [(e.relation, e.direction) for e in out.edges] == [("work_at", "out"), ("caus", "in")]


def test_facts_path_is_registered_read_only() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_tools
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    op = OPERATIONS_BY_NAME["facts.path"]
    ann = mcp_tools._tool_annotations(op)
    assert ann["readOnlyHint"] is True and ann["idempotentHint"] is True
    assert "facts.traverse" in TOOL_DESCRIPTIONS["facts.path"]
