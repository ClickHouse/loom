"""Every statement over memory_embeddings that carries a
`hasAnyTokens` predicate turns the text index's direct-read shortcut off.

On 26.6.1 the shortcut emits an empty block without the PREWHERE columns for
some token sets — code 10, deterministically — and a bounded retry cannot
absorb a deterministic failure. This pins the guard
on the vector leg (every mode that adds a lexical predicate), the lexical
recall fallback and the trace variant, by inspecting the SQL the service
builds; CH-free."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval import vector_search, vector_search_trace
from server.memory.domain.search_types import SearchQuery

SETTING = "query_plan_direct_read_from_text_index = 0"


class _RecordingCH:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql.append(sql)
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.sql.append(sql)
        return None


def _q(mode: str) -> SearchQuery:
    return SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="fake-ngram-1024",
        query_vec=[0.1] * 8,
        top_k=5,
        query_text="grace hash join bucket spilling bug",
        search_mode=mode,
        execution_planes=["fact", "lexical"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bm25_hybrid", "hybrid", "rrf", "semantic", "lexical"])
async def test_every_text_index_statement_reads_through_the_data(mode: str) -> None:
    ch = _RecordingCH()
    await vector_search(_q(mode), ch=ch)
    token_statements = [s for s in ch.sql if "hasAnyTokens" in s and "loom.memory_embeddings" in s]
    assert token_statements, f"{mode}: expected at least the lexical fallback to carry hasAnyTokens"
    missing = [s for s in token_statements if SETTING not in s]
    assert not missing, f"{mode}: {len(missing)} hasAnyTokens statement(s) without the guard"


@pytest.mark.asyncio
async def test_vector_leg_carries_the_guard_even_without_a_lexical_predicate() -> None:
    """The main vector statement always carries it: its SETTINGS block is one
    string, and a mode switch must not be able to drop the guard."""
    ch = _RecordingCH()
    await vector_search(_q("semantic"), ch=ch)
    main = [s for s in ch.sql if "vector_search_index_fetch_multiplier" in s]
    assert main and all(SETTING in s for s in main)


@pytest.mark.asyncio
async def test_trace_variant_carries_the_guard() -> None:
    ch = _RecordingCH()
    await vector_search_trace(_q("bm25_hybrid"), ch=ch)
    final_statements = [s for s in ch.sql if "loom.memory_embeddings AS e FINAL" in s]
    assert final_statements and all(SETTING in s for s in final_statements)
