"""The one-hop expansion is proximity-only: co-occurrence and authored edges,
no typed-triple vote."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import fetch_graph_neighbors


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        if "FROM loom.entity_degree" in sql:
            return []
        return [{"neighbor": "clickhouse"}]


async def _run(**kw: Any) -> tuple[list[str], _CH]:
    ch = _CH()
    out = await fetch_graph_neighbors(
        ch, org="o", namespace="n", seed_entities=["alice"], hub_skip_degree=0, **kw
    )
    return out, ch


@pytest.mark.asyncio
async def test_default_expansion_is_proximity_only() -> None:
    out, ch = await _run()
    sql, params = ch.calls[-1]
    assert out == ["clickhouse"]
    assert "loom.entity_edges" in sql and "loom.operator_edges" in sql
    assert "loom.entity_triples" not in sql
