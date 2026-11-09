"""The batched peer lookups bind each fact's vector as its own parameter: one
ClickHouse HTTP parameter is capped at 128 KiB, and six 3072-d vectors in one
array parameter are past it, which failed every batched lookup at production width."""

from __future__ import annotations

import random
from typing import Any

import pytest

from tests.integration._resources import unique_org


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


def _params(n: int, dim: int = 3072) -> dict[str, Any]:
    from server.memory.store.conflict_candidates import vector_params

    rnd = random.Random(7)
    vectors = [[rnd.uniform(-1, 1) for _ in range(dim)] for _ in range(n)]
    return {
        "org": unique_org("batch"),
        "ns": "batch",
        "dim": dim,
        **vector_params(vectors),
        "types": ["semantic"] * n,
        "keys": [f"k{i}" for i in range(n)],
        "max_dists": [0.4] * n,
        "min_cos": 0.6,
    }


async def test_a_full_batch_of_production_width_vectors_runs(ch) -> None:
    from server.memory.app.value_updates import _peers_sql_many
    from server.memory.store.conflict_candidates import candidate_sql_many, pinned_sql_many

    params = _params(6)
    assert await ch.query_all(candidate_sql_many(6), params=params) == []
    assert await ch.query_all(pinned_sql_many(6), params=params) == []
    assert await ch.query_all(_peers_sql_many(6), params=params) == []
