"""The code legs' body columns are fetched by the server's lazy
materialization inside the pool statement, so there is no separate
score-then-fetch step. Two premises of that are pinned here.

The lazy step exists only for `ORDER BY … LIMIT n` with n at or under
`query_plan_max_limit_for_lazy_materialization` (10,000 by default); above it
the legs would read every candidate's body eagerly (2x the lexical bytes and
4.4x the vector bytes). And a statement that forced the setting off would do
the same. Either regression fails here."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.search.state import _POOL, _POOL_KEEP
from server.code.store.index import CodeRepo

# ClickHouse's default for query_plan_max_limit_for_lazy_materialization.
LAZY_LIMIT_CAP = 10_000
BODY_COLUMNS = ("content", "symbol_ids", "context_description")


class _StubCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return []


def _norm(sql: str) -> str:
    return " ".join(sql.split())


async def _leg_statements() -> list[tuple[str, dict[str, Any]]]:
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.lexical_search_chunks("o", "n", "m", ["quota", "key"], [1.5, 0.7], pool=200)
    await repo.vector_search_chunks("o", "n", "m", [0.1, 0.2, 0.3], pool=200)
    assert len(ch.calls) == 2
    return ch.calls


def test_pool_depths_stay_under_the_lazy_materialization_cap() -> None:
    assert _POOL <= LAZY_LIMIT_CAP
    assert _POOL_KEEP <= _POOL


@pytest.mark.asyncio
async def test_each_leg_is_one_ordered_limited_statement_naming_its_bodies() -> None:
    for sql, params in await _leg_statements():
        flat = _norm(sql)
        select = flat.split(" FROM ", 1)[0]
        for col in BODY_COLUMNS:
            assert col in select, f"{col} left the SELECT: the lazy read is what fetches it\n{flat}"
        assert flat.count(" ORDER BY ") == 1 and " LIMIT {pool:UInt32}" in flat
        assert params["pool"] <= LAZY_LIMIT_CAP
        assert "query_plan_optimize_lazy_materialization" not in flat
        assert "PREWHERE" not in flat
