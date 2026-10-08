"""The main search arm reads its candidates in one query.

CH-free: the main search arm against a stub client.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import vector_search
from server.memory.domain.search_types import SearchQuery


class _CH:
    def __init__(self, first: list[dict[str, Any]], second: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.first, self.second = first, second

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        if "FROM loom.memory_embeddings AS e" not in sql:
            return []
        return self.second if "cand_keys" in sql else self.first


def _row(key: str, distance: float, version: int = 1) -> dict[str, Any]:
    return {
        "memory_key": key,
        "memory_type": "semantic",
        "content_excerpt": f"v{version} of {key}",
        "distance": distance,
        "rank_score": 1 - distance,
        "temporal_anchor": "",
        "valid_at": "",
        "created_at": "",
        "source_resource_id": "",
    }


def _q() -> SearchQuery:
    return SearchQuery(
        org="o",
        namespace="n",
        agent="a",
        scope="private",
        embedding_model="fake",
        query_vec=[0.1] * 4,
        top_k=5,
        search_mode="semantic",
        execution_planes=("fact",),
    )


@pytest.mark.asyncio
async def test_the_fact_leg_is_one_query() -> None:
    ch = _CH([_row("k1", 0.1), _row("k2", 0.2)], [])
    hits = await vector_search(_q(), ch=ch)
    ann = [c for c in ch.calls if "FROM loom.memory_embeddings AS e" in c[0]]
    assert len(ann) == 1 and "cand_keys" not in ann[0][0]
    assert [h.memory_key for h in hits] == ["k1", "k2"]
