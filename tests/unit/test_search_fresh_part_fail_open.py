"""A fresh-part ClickHouse error that survives the vector leg's retries fails
open per namespace with a `degraded` note, instead of a 500 on the whole ask
(fresh parts right after writes can raise it)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.memory.app.search import vector_legs as hs
from server.observability import degradations


def _ctx() -> SimpleNamespace:
    inp = SimpleNamespace(
        org="o",
        scope="private",
        agent="*",
        memory_type=None,
        user_id=None,
        session_id=None,
        run_id=None,
        tags=[],
        query="q",
        search_mode="rrf",
        alpha=0.5,
        include_top_n_unmatched=0,
        namespace="n",
        as_of=None,
        as_of_basis="valid_time",
        recipe="",
    )
    return SimpleNamespace(
        input=inp,
        actual_work={"vector_queries": 0},
        embedder=SimpleNamespace(model_name="m"),
        query_vec=[0.1],
        pool_top_k=5,
        effective_entity_filter=None,
    )


def test_fresh_part_error_fails_open_with_a_degraded_note(monkeypatch) -> None:
    async def _boom(q):
        raise RuntimeError(
            "Code: 10. DB::Exception: Not found column org: in block memory_key String String(size = 0)"
        )

    monkeypatch.setattr("server.memory.app.retrieval.vector_search", _boom)
    monkeypatch.setattr(hs, "_search_query_for", lambda ctx, ns: object())
    degradations.begin()
    ns, hits = asyncio.run(hs._search_one(_ctx(), "n"))
    assert (ns, hits) == ("n", [])
    assert any("vector_leg_empty" in d for d in degradations.current())


def test_other_errors_still_raise(monkeypatch) -> None:
    async def _boom(q):
        raise RuntimeError("Code: 241. DB::Exception: Memory limit exceeded")

    monkeypatch.setattr("server.memory.app.retrieval.vector_search", _boom)
    monkeypatch.setattr(hs, "_search_query_for", lambda ctx, ns: object())
    with pytest.raises(RuntimeError):
        asyncio.run(hs._search_one(_ctx(), "n"))
