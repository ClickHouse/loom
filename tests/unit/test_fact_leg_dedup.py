"""The fact leg merges versions with FINAL (revised: its
identity PREWHERE makes FINAL the cheaper plan), live and under replay."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.app.retrieval import vector_search
from server.memory.domain.search_types import SearchQuery


class _CH:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append(sql)
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        return None


def _q(**kw: Any) -> SearchQuery:
    base: dict[str, Any] = dict(
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
    base.update(kw)
    return SearchQuery(**base)


def _fact_leg(ch: _CH) -> str:
    legs = [s for s in ch.calls if "loom.memory_embeddings AS e" in s and "cosineDistance" in s]
    assert legs, ch.calls
    return legs[0]


@pytest.mark.asyncio
async def test_live_reads_keep_final() -> None:
    ch = _CH()
    await vector_search(_q(), ch=ch)  # type: ignore[arg-type]
    sql = _fact_leg(ch)
    assert "FROM loom.memory_embeddings AS e FINAL" in sql
    assert "max(version)" not in sql


@pytest.mark.asyncio
async def test_replay_reads_keep_final() -> None:
    ch = _CH()
    await vector_search(_q(as_of=datetime(2026, 1, 1)), ch=ch)  # type: ignore[arg-type]
    sql = _fact_leg(ch)
    assert "FROM loom.memory_embeddings AS e FINAL" in sql
    assert "max(version)" not in sql
