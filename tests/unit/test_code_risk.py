"""code_risk annotation: per-result churn/amp/tested/callees.

Two contracts worth pinning: (1) annotations NEVER exceed the reservation
the handler carved out of the request budget — callees drop tail-rank-first
to fit, risk ints survive; (2) symbol attribution is by path + line overlap,
so a unit never inherits risk from an unrelated function in the same file.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.risk import annotate_units, reserve_for
from server.code.domain.units import CodeUnit


class _FakeRepo:
    def __init__(self) -> None:
        self.edge_calls: list[dict[str, Any]] = []
        self.churn = {"src/A.cpp": 12}
        self.tests = [{"src_path": "src/A.cpp", "test_path": "tests/a_test.cpp", "confidence": 1.0}]
        self.symbols = [
            {
                "symbol_id": "sA",
                "qualified_name": "DB::A::run",
                "name": "run",
                "signature": "",
                "path": "src/A.cpp",
                "start_line": 10,
                "end_line": 40,
                "tier": "scip",
            },
            {
                "symbol_id": "sOther",
                "qualified_name": "DB::A::other",
                "name": "other",
                "signature": "",
                "path": "src/A.cpp",
                "start_line": 100,
                "end_line": 130,
                "tier": "scip",
            },
        ]
        self.caller_edges = [
            {
                "to_symbol_id": "sA",
                "from_symbol_id": "c1",
                "from_qualified_name": "DB::Caller::one",
                "to_qualified_name": "DB::A::run",
                "path": "src/C.cpp",
                "line": 5,
                "tier": "scip",
                "edge_kind": "calls",
                "confidence": 1.0,
            },
            {
                "to_symbol_id": "sA",
                "from_symbol_id": "c2",
                "from_qualified_name": "DB::Caller::two",
                "to_qualified_name": "DB::A::run",
                "path": "src/C.cpp",
                "line": 9,
                "tier": "scip",
                "edge_kind": "calls",
                "confidence": 1.0,
            },
        ]
        self.callee_edges = [
            {
                "from_symbol_id": "sA",
                "to_symbol_id": "z1",
                "from_qualified_name": "DB::A::run",
                "to_qualified_name": "DB::Z::helper",
                "path": "src/Z.cpp",
                "line": 77,
                "tier": "scip",
                "edge_kind": "calls",
                "confidence": 1.0,
            },
        ]

    async def pr_churn_for(self, org, ns, paths, *, days=365):
        return {p: n for p, n in self.churn.items() if p in paths}

    async def tested_paths(self, org, ns, paths=None):
        return {t["src_path"] for t in self.tests if paths is None or t["src_path"] in paths}

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [s for s in self.symbols if s["path"] in paths]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, per_match=None
    ):
        self.edge_calls.append({"direction": direction, "per_match": per_match, "limit": limit})
        rows = self.caller_edges if direction == "callers" else self.callee_edges
        key = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        return [r for r in rows if r[key] in ids]


def _unit(**kw: Any) -> CodeUnit:
    base = dict(
        unit_id="ch1",
        path="src/A.cpp",
        start_line=12,
        end_line=35,
        symbol="DB::A::run",
        kind="function",
    )
    base.update(kw)
    return CodeUnit(**base)


@pytest.mark.asyncio
async def test_annotates_churn_amp_tested_and_callees() -> None:
    u = _unit()
    spent = await annotate_units(_FakeRepo(), "o", "n", [u], reserve_tokens=reserve_for(5))
    assert u.churn == 12
    assert u.amp == 2  # two distinct caller symbols of sA
    assert u.tested is True
    assert [c["name"] for c in u.callees] == ["DB::Z::helper"]
    assert 0 < spent <= reserve_for(5)


@pytest.mark.asyncio
async def test_line_overlap_gates_symbol_attribution() -> None:
    # Unit covers 100-130 -> sOther, which has no edges: amp 0, no callees,
    # but path-level facts (churn, tested) still apply.
    u = _unit(start_line=100, end_line=130, symbol="DB::A::other")
    await annotate_units(_FakeRepo(), "o", "n", [u], reserve_tokens=reserve_for(5))
    assert u.amp == 0 and u.callees == []
    assert u.churn == 12 and u.tested is True


@pytest.mark.asyncio
async def test_symbol_kind_unit_uses_its_own_id() -> None:
    u = _unit(unit_id="sA", kind="symbol")
    await annotate_units(_FakeRepo(), "o", "n", [u], reserve_tokens=reserve_for(5))
    assert u.amp == 2


@pytest.mark.asyncio
async def test_callees_drop_tail_first_under_tight_reserve() -> None:
    # Reserve fits the risk ints but not both callee blocks: rank-2 loses
    # its callees first; ints survive on every unit.
    repo = _FakeRepo()
    u1, u2 = _unit(), _unit(unit_id="ch2")
    spent = await annotate_units(repo, "o", "n", [u1, u2], reserve_tokens=18)
    assert spent <= 18
    assert u1.churn == 12 and u2.churn == 12  # ints always land
    assert u2.callees == []  # tail rank dropped first


@pytest.mark.asyncio
async def test_empty_units_is_free() -> None:
    assert await annotate_units(_FakeRepo(), "o", "n", [], reserve_tokens=100) == 0


def test_reserve_is_capped() -> None:
    assert reserve_for(25) <= 600


@pytest.mark.asyncio
async def test_caller_and_callee_hops_are_read_together_and_callees_are_capped_per_source() -> None:
    """The two edge hops are independent reads of one table,
    so they are awaited together (one round trip of wall time, not two), and
    the callee read is capped per source symbol in SQL so a hub cannot spend
    the flat limit on itself."""
    import asyncio

    from server.code.app import risk as code_risk

    class _Gate(_FakeRepo):
        def __init__(self) -> None:
            super().__init__()
            self.callers_started = asyncio.Event()
            self.callees_started = asyncio.Event()

        async def edges_for_symbols(self, org, ns, ids, *, direction, **kw):
            # Each hop only completes once the OTHER hop has started: a
            # sequential await would deadlock here and the test would time out.
            if direction == "callers":
                self.callers_started.set()
                await asyncio.wait_for(self.callees_started.wait(), 1.0)
            else:
                self.callees_started.set()
                await asyncio.wait_for(self.callers_started.wait(), 1.0)
            return await super().edges_for_symbols(org, ns, ids, direction=direction, **kw)

    repo = _Gate()
    units = [_unit()]
    await code_risk.annotate_units(repo, "o", "n", units, reserve_tokens=400)
    by_dir = {c["direction"]: c for c in repo.edge_calls}
    assert set(by_dir) == {"callers", "callees"}
    assert by_dir["callers"]["per_match"] is None  # amp counts every caller
    assert by_dir["callees"]["per_match"] == code_risk.CALLEES_PER_SOURCE_READ
    assert code_risk.CALLEES_PER_SOURCE_READ >= code_risk.CALLEES_PER_UNIT * 2
    assert units[0].callees and units[0].callees[0]["name"] == "DB::Z::helper"
