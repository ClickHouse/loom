"""The RRF route runs no lexical union pool.

On a facts-only namespace the pool's statement -- a plain FINAL scan of the
namespace with the bf16 vector, `ORDER BY lex_score DESC, distance ASC` -- is
the whole `vector_legs` stage, costlier than the codes-first fact leg. RRF
fuses the fact leg's own lexical rank with its semantic rank instead; a blind
judged pair chose that. Every other route that carries the lexical plane keeps the pool.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import vector_search
from server.memory.domain.search_types import SearchQuery


@pytest.fixture(autouse=True)
def _key_axis_pinned(monkeypatch: pytest.MonkeyPatch):
    from server.settings import settings as _pinned

    monkeypatch.setattr(_pinned, "memory_search_key_axis", "off")
    yield


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return []


def _q(**overrides: Any) -> SearchQuery:
    base: dict[str, Any] = {
        "org": "clickhouse",
        "namespace": "clickgap-public",
        "scope": "private",
        "agent": "clickgap",
        "embedding_model": "text-embedding-3-large",
        "query_vec": [0.1] * 3072,
        "query_text": "src Storages MergeTree DataPartsExchange cpp retry on fetch",
        "top_k": 30,
        "search_mode": "rrf",
    }
    base.update(overrides)
    return SearchQuery(**base)


def _pool_statements(ch: StubCH) -> list[str]:
    return [sql for sql, _ in ch.query_all_calls if "AS lex_score" in sql]


def _fact_statement(ch: StubCH) -> str:
    return next(
        sql for sql, _ in ch.query_all_calls if "AS rank_score" in sql and "AS lex_score" not in sql
    )


@pytest.mark.asyncio
async def test_rrf_issues_no_pool_and_keeps_its_own_lexical_rank() -> None:
    ch = StubCH()
    await vector_search(_q(), ch=ch)
    assert _pool_statements(ch) == []
    assert "AS lexical_sql" in _fact_statement(ch)


@pytest.mark.asyncio
async def test_non_rrf_routes_keep_the_pool() -> None:
    """The identifier route and the explicit lexical modes
    reach a UUID or hash only through the pool."""
    for mode in ("lexical", "bm25_hybrid"):
        ch = StubCH()
        await vector_search(_q(search_mode=mode), ch=ch)
        assert len(_pool_statements(ch)) == 1, mode
        assert "ORDER BY lex_score DESC, distance ASC, e.memory_key ASC" in _pool_statements(ch)[0]


@pytest.mark.asyncio
async def test_plane_mask_still_wins() -> None:
    """`execution_planes` without `lexical` never runs the pool (the planner's
    `fast` recipe)."""
    ch = StubCH()
    await vector_search(_q(search_mode="lexical", execution_planes=["fact"]), ch=ch)
    assert _pool_statements(ch) == []
