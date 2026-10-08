"""The test-map page is deterministic.

`test_map_for` orders by `confidence DESC, src_path, test_path` (and
`test_map_detail` by `confidence DESC, src_path, test_path, method`), so a
page cut from rows that all tie at one confidence is the same page on every
process; and `code.impact` hands the read its paths in a fixed order —
seeds first, then the radius, each sorted — because the read cuts at 50
paths and a set's iteration order is per process.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import patch

import pytest

from tests.unit.test_code_structural_verdicts import (  # noqa: F401 — the fixture pins org/ns
    _intel_on,
    _Repo,
)


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows or []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        return list(self._rows)


def _order_by(sql: str) -> str:
    m = re.search(r"ORDER BY\s+(.*?)\s+LIMIT", " ".join(sql.split()), re.S)
    assert m, sql
    return m.group(1)


def test_test_map_for_orders_the_tied_rows_by_path() -> None:
    from server.code.store.index import CodeRepo

    ch = _CH()
    asyncio.run(CodeRepo(ch).test_map_for("o", "n", ["src/B.cpp", "src/A.cpp"]))  # type: ignore[arg-type]
    ((sql, params),) = ch.calls
    assert _order_by(sql) == "confidence DESC, src_path, test_path"
    # the cut is the caller's order, not a re-sort inside the read
    assert params["paths"] == ["src/B.cpp", "src/A.cpp"] and params["limit"] == 30


def test_test_map_detail_orders_the_tied_rows_by_path_and_method() -> None:
    from server.code.store.index import CodeRepo

    ch = _CH()
    asyncio.run(CodeRepo(ch).test_map_detail("o", "n", ["src/A.cpp"]))  # type: ignore[arg-type]
    ((sql, _),) = ch.calls
    assert _order_by(sql) == "confidence DESC, src_path, test_path, method"


def test_no_test_map_page_is_cut_from_a_tied_order() -> None:
    """Every `code_test_map` read that pages (`LIMIT`) after an `ORDER BY`
    closes the order with a key column (or the column it groups by): a tie is a
    per-process page."""
    from pathlib import Path

    src = "".join(
        p.read_text(encoding="utf-8") for p in sorted(Path("server/code/store/index").glob("*.py"))
    )
    stmts = [
        s
        for s in re.split(r'"""', src)
        if "loom.code_test_map" in s and "ORDER BY" in s and "LIMIT" in s
    ]
    assert stmts, "the test-map readers moved"
    for s in stmts:
        order = _order_by(s)
        grouped_by_test = re.search(r"GROUP BY test_path\b", s) and re.search(
            r"\btest_path\b", order
        )
        assert re.search(r"\bsrc_path\b", order) or grouped_by_test, s


def test_impact_hands_the_read_its_paths_seeds_first_then_sorted() -> None:
    from server.code.app.symbol.impact import _test_map_paths

    impacted = {
        "s1": {"path": "src/Z.cpp"},
        "s2": {"path": "src/A.cpp"},
        "s3": {"path": "src/M.cpp"},
        "s4": {"path": "src/Seed.cpp"},  # the seed's own file in the radius counts once
    }
    a = _test_map_paths(["src/Seed.cpp", "src/Other.cpp"], impacted)
    # the same inputs with the dict and list in another order: the same paths
    b = _test_map_paths(["src/Other.cpp", "src/Seed.cpp"], dict(reversed(list(impacted.items()))))
    assert a == b == ["src/Other.cpp", "src/Seed.cpp", "src/A.cpp", "src/M.cpp", "src/Z.cpp"]
    # a radius wider than the read's 50-path cut never pushes a seed out
    wide = {f"s{i}": {"path": f"src/{i:03d}.cpp"} for i in range(80)}
    out = _test_map_paths(["src/zzz_seed.cpp"], wide)
    assert out[0] == "src/zzz_seed.cpp" and len(out) == 81 and out[1:] == sorted(out[1:])


def test_impacted_tests_follow_the_read_order_on_a_tie(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler keeps the page as the read ordered it (no re-sort, no set),
    so a total order in SQL is a total order on the wire."""
    from server.code.api.models_symbol import CodeImpactInput
    from server.code.app.symbol import impact as h

    tied = [
        {
            "src_path": "src/A.cpp",
            "test_path": "tests/queries/0_stateless/2.sql",
            "confidence": 0.9,
        },
        {
            "src_path": "src/A.cpp",
            "test_path": "tests/queries/0_stateless/1.sql",
            "confidence": 0.9,
        },
    ]

    class Repo(_Repo):
        seen: list[list[str]] = []

        async def test_map_for(self, org: str, ns: str, paths: list[str]) -> list[dict[str, Any]]:
            self.seen.append(list(paths))
            return tied  # the read's order stands in for ClickHouse's

    async def _none(*a: Any, **k: Any) -> Any:
        return []

    async def _zero(*a: Any, **k: Any) -> tuple[int, int]:
        return 0, 0

    monkeypatch.setattr(h, "schedule_code_trace", lambda **k: None)
    monkeypatch.setattr(h, "_dispatch_seeds", _none)
    monkeypatch.setattr(h, "_declined_safe", _zero)
    with patch.object(h, "CodeRepo", return_value=Repo()):
        out = asyncio.run(h.handle_code_impact(CodeImpactInput(name="DB::A::f")))
    assert [t["test_path"] for t in out.impacted_tests] == [
        "tests/queries/0_stateless/2.sql",
        "tests/queries/0_stateless/1.sql",
    ]
    (paths,) = Repo.seen
    assert paths == sorted(paths) and paths[0] == "src/A.cpp"
