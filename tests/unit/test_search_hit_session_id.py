"""A fact hit names the session it came from.

The dense and lexical legs project `e.session_id` and carry it on the
service hit; the handler copies it to the wire model. Summary / chunk /
derived rows have none and read as "".
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.retrieval import vector_search
from server.memory.domain.search_types import SearchHit, SearchQuery


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.calls: list[str] = []
        self.rows = rows

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append(sql)
        if "FROM loom.memory_embeddings AS e" not in sql:
            return []
        return self.rows


def _row(key: str, distance: float, session_id: str) -> dict[str, Any]:
    return {
        "memory_key": key,
        "memory_type": "semantic",
        "content_excerpt": f"about {key}",
        "distance": distance,
        "rank_score": 1 - distance,
        "temporal_anchor": "",
        "valid_at": "",
        "created_at": "",
        "source_resource_id": "",
        "session_id": session_id,
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
async def test_dense_leg_projects_and_carries_session_id() -> None:
    ch = _CH([_row("k1", 0.1, "n:session_3"), _row("k2", 0.2, "")])
    hits = await vector_search(_q(), ch=ch)
    fact_sql = next(s for s in ch.calls if "FROM loom.memory_embeddings AS e" in s)
    assert "e.session_id AS session_id" in fact_sql
    by_key = {h.memory_key: h for h in hits}
    assert by_key["k1"].session_id == "n:session_3"
    assert by_key["k2"].session_id == ""  # a row without one reads as "", never None


def test_service_and_wire_hits_default_to_no_session() -> None:
    h = SearchHit(memory_key="k", memory_type="semantic", content_excerpt="x", distance=0.2)
    assert h.session_id == ""
    w = _MemorySearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt="x",
        distance=0.2,
        similarity=0.8,
        session_id="n:session_9",
    )
    assert w.session_id == "n:session_9"
    assert _MemorySearchHit.model_fields["session_id"].default == ""
