"""An external reranker's order is the order memory.search returns
(`server/memory/app/search/ranking.py::_run_external_rerank`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search import ranking
from server.memory.domain.context_block import assemble_context_block


def _hit(key: str, score: float, session: str = "s1") -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"{key} in {session}",
        distance=1.0 - score,
        similarity=score,
        rank_score=score,
        source="fact",
        session_id=session,
        namespace="n",
    )


def _ctx(hits: list[_MemorySearchHit]) -> Any:
    return SimpleNamespace(
        input=SimpleNamespace(query="where does the user live now?", namespace="n"),
        endpoint="builtin:openai",
        prompt_template="",
        all_hits=hits,
        chronological_intent=None,
        actual_work={},
    )


def _reranker(order: list[str]) -> Any:
    """A reranker that returns the candidates whose excerpts are listed, in that order."""

    async def _rerank(*, candidates: list[Any], **_k: Any) -> list[Any]:
        by_text = {c.content_excerpt: c for c in candidates}
        return [by_text[t] for t in order]

    return _rerank


def _block(ctx: Any) -> list[str]:
    return [h.content_excerpt for h in assemble_context_block(ctx.all_hits, top_k=10)]


@pytest.mark.asyncio
async def test_the_block_keeps_the_rerankers_order(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx([_hit("user_name", 0.40), _hit("user_editor", 0.37), _hit("user_rust", 0.20)])
    order = ["user_editor in s1", "user_rust in s1", "user_name in s1"]
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(order))
    await ranking._run_external_rerank(ctx)
    assert _block(ctx) == order
    # The scores are the pool's own, handed out in the reranker's order.
    assert [h.rank_score for h in ctx.all_hits] == [0.40, 0.37, 0.20]
    assert ctx.all_hits[0].score_components["rank_score_pre_rerank"] == 0.37


@pytest.mark.asyncio
async def test_rows_of_one_key_from_two_sessions_both_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction writes a key once per session: the reranker ranks each row."""
    ctx = _ctx([_hit("user_location", 0.37, "s1"), _hit("user_location", 0.33, "s3")])
    order = ["user_location in s3", "user_location in s1"]
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(order))
    await ranking._run_external_rerank(ctx)
    assert _block(ctx) == order
