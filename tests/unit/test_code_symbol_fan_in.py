"""Symbol fan-in — the weekly rows, the field, the reads and
the optional capped prior. CH-free."""

from __future__ import annotations

import math
from typing import Any

import pytest

from server.code.app import fan_in as code_fan_in
from server.code.app.search.priors import _COHERENCE_FRAC, _apply_fusion_signals
from server.code.domain.units import CodeUnit
from server.code.store.index import CODE_SYMBOL_STATS_COLS, build_row
from server.code.workers.graph_stats import compute_symbol_stats


def _agg(sid: str, qn: str, calls: int, nontest: int, files: int) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": qn,
        "fan_in_calls": calls,
        "fan_in_nontest": nontest,
        "caller_files": files,
    }


# ---- the weekly rows -------------------------------------------------------


def test_stats_row_has_every_persisted_column_and_no_created_at() -> None:
    row = build_row("code_symbol_stats", org="o", namespace="n", symbol_id="s")
    assert set(row) == set(CODE_SYMBOL_STATS_COLS)
    assert "created_at" not in row
    assert row["computed_at"] is not None and row["version"] > 0


def test_external_symbols_are_dropped_before_the_write() -> None:
    rows = compute_symbol_stats(
        "o",
        "n",
        [
            _agg("a", "std::__1::shared_ptr::operator->", 44988, 39469, 2651),
            _agg("b", "boost::intrusive_ptr::operator->", 17839, 15527, 1769),
            _agg("c", "testing::Message::Message", 15819, 0, 476),
            _agg("d", "DB::Exception::Exception", 14445, 14380, 2453),
            _agg("e", "", 5, 5, 1),
        ],
        {"d": (1, 0, "src/Common/Exception.h")},
    )
    assert [r["symbol_id"] for r in rows] == ["d"]
    r = rows[0]
    assert r["qualified_name"] == "DB::Exception::Exception"
    assert r["path"] == "src/Common/Exception.h"
    assert (r["fan_in_calls"], r["fan_in_nontest"], r["caller_files"]) == (14445, 14380, 2453)
    assert r["defined_files"] == 1 and r["is_test"] == 0


def test_test_defined_symbol_is_flagged_and_undefined_symbol_keeps_zero_sites() -> None:
    rows = compute_symbol_stats(
        "o",
        "n",
        [
            _agg(
                "t", "tests.integration.helpers.cluster::ClickHouseInstance::query", 18896, 0, 798
            ),
            _agg("m", "FormatStringHelperImpl::FormatStringHelperImpl", 14761, 14693, 2483),
        ],
        {"t": (1, 1, "tests/integration/helpers/cluster.py")},
    )
    by = {r["symbol_id"]: r for r in rows}
    assert by["t"]["is_test"] == 1 and by["t"]["fan_in_nontest"] == 0
    assert by["m"]["defined_files"] == 0 and by["m"]["path"] == "" and by["m"]["is_test"] == 0


def test_one_pass_shares_one_version_and_retires_vanished_symbols_at_zero() -> None:
    rows = compute_symbol_stats(
        "o",
        "n",
        [_agg("a", "DB::f", 3, 3, 2)],
        {},
        previous_ids={"a", "gone1", "gone2"},
    )
    assert [r["symbol_id"] for r in rows] == ["a", "gone1", "gone2"]
    assert len({r["version"] for r in rows}) == 1
    assert len({r["computed_at"] for r in rows}) == 1
    retired = [r for r in rows if r["symbol_id"].startswith("gone")]
    assert all(r["fan_in_calls"] == 0 and r["fan_in_nontest"] == 0 for r in retired)
    assert all(r["org"] == "o" and r["namespace"] == "n" for r in rows)


def test_fan_in_name_is_the_most_called_id_sharing_the_qualified_name() -> None:
    # two tiers / an overload set: one row per id, the name reads as its max
    rows = compute_symbol_stats(
        "o",
        "n",
        [
            _agg("scip1", "DB::FormatFactory::instance", 150, 144, 60),
            _agg("ts1", "DB::FormatFactory::instance", 5, 5, 3),
            _agg("x", "DB::other", 2, 2, 1),
        ],
        {},
    )
    by = {r["symbol_id"]: r for r in rows}
    assert (by["scip1"]["fan_in_nontest"], by["scip1"]["fan_in_name"]) == (144, 144)
    assert (by["ts1"]["fan_in_nontest"], by["ts1"]["fan_in_name"]) == (5, 144)
    assert by["x"]["fan_in_name"] == 2


def test_defined_files_is_clamped_to_uint16() -> None:
    rows = compute_symbol_stats("o", "n", [_agg("a", "DB::f", 1, 1, 1)], {"a": (70000, 0, "x")})
    assert rows[0]["defined_files"] == 65535


# ---- the field -------------------------------------------------------------


def test_fan_in_is_dropped_from_the_wire_when_zero_and_present_otherwise() -> None:
    assert "fan_in" not in CodeUnit(path="a.cpp").model_dump()
    assert CodeUnit(path="a.cpp", fan_in=7).model_dump()["fan_in"] == 7


# ---- the reads -------------------------------------------------------------


class _Repo:
    def __init__(
        self, stats: dict[str, dict[str, Any]], symbols: list[dict[str, Any]] | None = None
    ):
        self.stats = stats
        self.symbols = symbols or []
        self.calls: list[tuple[str, Any]] = []

    async def symbol_stats(self, org: str, ns: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        self.calls.append(("symbol_stats", list(ids)))
        return {i: self.stats[i] for i in ids if i in self.stats}

    async def top_symbol_stats(
        self,
        org: str,
        ns: str,
        *,
        path_prefix: str = "",
        limit: int = 10,
        include_tests: bool = False,
    ) -> list[dict[str, Any]]:
        self.calls.append(("top_symbol_stats", path_prefix))
        rows = [
            r for r in self.stats.values() if r["path"].startswith(path_prefix) and not r["is_test"]
        ]
        rows.sort(key=lambda r: -r["fan_in_nontest"])
        return rows[:limit]

    async def symbols_by_ids(self, org: str, ns: str, ids: list[str]) -> list[dict[str, Any]]:
        return [s for s in self.symbols if s["symbol_id"] in ids]


def _stat(sid: str, qn: str, path: str, nontest: int, is_test: int = 0) -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": qn,
        "path": path,
        "fan_in_calls": nontest + 1,
        "fan_in_nontest": nontest,
        "caller_files": 3,
        "defined_files": 1,
        "is_test": is_test,
    }


@pytest.mark.asyncio
async def test_annotate_maps_chunks_through_symbol_ids_and_symbol_units_through_their_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(
        {
            "s1": _stat("s1", "DB::A::f", "src/A.cpp", 40),
            "s2": {**_stat("s2", "DB::B::g", "src/B.cpp", 3), "fan_in_name": 9},
            "sB": _stat("sB", "DB::B", "src/B.cpp", 2),
        }
    )
    chunk = CodeUnit(unit_id="c1", path="src/A.cpp", symbol="DB::A::f")
    # a class skeleton defines its members: it reads its OWN row when one
    # exists, and 0 when only members have callers — never a member's count
    skeleton = CodeUnit(unit_id="c2", path="src/B.cpp", symbol="DB::B")
    skeleton_no_own = CodeUnit(unit_id="c4", path="src/B.cpp", symbol="DB::B")
    # e.g. IQueryTreeNode's skeleton has ONE member with callers
    # (getNodeType, 239) among many members without — still not the class's
    skeleton_one_member = CodeUnit(unit_id="c5", path="src/B.cpp", symbol="DB::B")
    symbol_unit = CodeUnit(unit_id="s2", path="src/B.cpp", symbol="DB::B::g", kind="symbol")
    other = CodeUnit(unit_id="c3", path="src/C.cpp")
    n = await code_fan_in.annotate_units(
        repo,
        "o",
        "n",
        [chunk, skeleton, skeleton_no_own, skeleton_one_member, symbol_unit, other],
        {"c1": ["s1"], "c2": ["sB", "s1", "s2"], "c4": ["s1", "s2"], "c5": ["zz", "s2"], "c3": []},
    )
    assert n == 3
    assert (chunk.fan_in, skeleton.fan_in, skeleton_no_own.fan_in) == (40, 2, 0)
    assert (skeleton_one_member.fan_in, symbol_unit.fan_in, other.fan_in) == (0, 9, 0)
    assert repo.calls == [("symbol_stats", ["s1", "sB", "s2", "zz", "c3"])]


@pytest.mark.asyncio
async def test_annotate_fails_open_to_zero() -> None:
    class Broken:
        async def symbol_stats(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("table missing")

    u = CodeUnit(unit_id="c1", path="a.cpp")
    assert await code_fan_in.annotate_units(Broken(), "o", "n", [u], {"c1": ["s"]}) == 0
    assert u.fan_in == 0


@pytest.mark.asyncio
async def test_hub_units_are_bodyless_symbol_units_merged_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(
        {
            "s1": _stat("s1", "DB::A::f", "src/X/A.cpp", 40),
            "s2": _stat("s2", "DB::B::g", "src/X/B.cpp", 9),
        },
        symbols=[
            {
                "symbol_id": "s1",
                "qualified_name": "DB::A::f",
                "path": "src/X/A.cpp",
                "start_line": 10,
                "end_line": 20,
                "signature": "void f()",
                "tier": "scip",
            }
        ],
    )
    hubs = await code_fan_in.hub_units(repo, "o", "n", "src/X/", limit=5)
    assert [(h.unit_id, h.fan_in, h.kind, h.body, h.signature) for h in hubs] == [
        ("s1", 40, "symbol", "", "void f()"),
        ("s2", 9, "symbol", "", ""),
    ]
    retrieved = CodeUnit(
        unit_id="c9", path="src/X/A.cpp", symbol="DB::A::f", body="void f() {}", score=0.03
    )
    merged, added = code_fan_in.merge_hubs([retrieved], hubs)
    assert added == 1 and [u.unit_id for u in merged] == ["c9", "s2"]
    assert retrieved.fan_in == 40  # the retrieved unit keeps its body, takes the hub's fan_in
    low = CodeUnit(unit_id="c8", path="src/X/A.cpp", symbol="DB::A::f", fan_in=3, score=0.02)
    merged2, added2 = code_fan_in.merge_hubs([low], hubs)
    assert added2 == 1 and low.fan_in == 40  # the chunk's per-id count never hides the name's
    ordered = code_fan_in.order_by_fan_in(merged)
    assert [u.unit_id for u in ordered] == ["c9", "s2"]


def test_order_by_fan_in_breaks_ties_on_score() -> None:
    a = CodeUnit(unit_id="a", path="a", fan_in=5, score=0.1)
    b = CodeUnit(unit_id="b", path="b", fan_in=5, score=0.3)
    c = CodeUnit(unit_id="c", path="c", fan_in=0, score=0.9)
    assert [u.unit_id for u in code_fan_in.order_by_fan_in([a, c, b])] == ["b", "a", "c"]


# ---- the prior -------------------------------------------------------------


def test_prior_factor_is_capped_monotone_and_off_at_zero() -> None:
    assert code_fan_in.prior_factor(100, 100, 0.0) == 1.0
    assert code_fan_in.prior_factor(0, 100, 0.25) == 1.0
    assert code_fan_in.prior_factor(100, 100, 0.25) == pytest.approx(1.25)
    assert (
        1.0
        < code_fan_in.prior_factor(1, 100, 0.25)
        < code_fan_in.prior_factor(50, 100, 0.25)
        < 1.25
    )
    assert code_fan_in.prior_factor(7, 100, 0.5) == pytest.approx(
        1 + 0.5 * math.log1p(7) / math.log1p(100)
    )


def test_fusion_prior_lifts_hub_under_cap_and_skips_tests_and_noise() -> None:
    hub = CodeUnit(
        unit_id="h",
        path="src/Interpreters/Context.cpp",
        symbol="DB::Context::getSettingsRef",
        score=0.030,
        fan_in=1706,
    )
    leaf = CodeUnit(
        unit_id="l",
        path="src/Processors/Merges/Merge.cpp",
        symbol="DB::Merge::run",
        score=0.031,
        fan_in=1,
    )
    noisy = CodeUnit(
        unit_id="t",
        path="src/Interpreters/tests/gtest_ctx.cpp",
        symbol="TEST",
        score=0.031,
        fan_in=1706,
    )
    off = _apply_fusion_signals([leaf, hub], "zzz qqq")
    assert [u.unit_id for u in off] == ["l", "h"]  # cap 0 (stats not read): order unchanged
    on = _apply_fusion_signals([leaf, hub, noisy], "zzz qqq", prior_cap=0.25)
    assert on[0].unit_id == "h"
    by = {u.unit_id: u for u in on}
    # the whole cap, times its own coherence share of the pool max, nothing more
    assert by["h"].score <= 0.030 * 1.25 * (1 + _COHERENCE_FRAC) + 1e-9
    assert by["t"].score < by["l"].score  # noise path: penalised, never boosted
    intent = _apply_fusion_signals([leaf, hub], "how are the tests wired", prior_cap=0.25)
    assert [u.unit_id for u in intent] == ["l", "h"]  # test intent: prior skipped
