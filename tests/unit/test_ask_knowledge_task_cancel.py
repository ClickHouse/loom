"""memory.ask starts its knowledge-plane read before retrieval; an ask that
returns without reading that block must not leave the read running."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app import precedents as memory_precedents
from server.memory.app.ask import pipeline


@pytest.mark.asyncio
async def test_a_fast_precedent_answer_cancels_the_knowledge_read(monkeypatch) -> None:

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_passages(*_a, **_k):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "", 0

    async def fast_reuse(*_a, **_k):
        await started.wait()
        return memory_precedents.Precedent(
            precedent_key="k",
            question="q",
            answer="Stored answer [a].",
            cited_keys=["a"],
            evidence=None,  # type: ignore[arg-type]
            retrieved_at=datetime.now(UTC),
        )

    monkeypatch.setattr(pipeline, "knowledge_passages", slow_passages)
    monkeypatch.setattr(memory_precedents, "fast_reuse", fast_reuse)

    si = MemorySearchInput(org="o", namespace="n", agent="*", query="q")
    result = await pipeline.ask(si, llm=None, precedent_key="k", knowledge=True)

    assert result.source == "precedent"
    await asyncio.wait_for(cancelled.wait(), timeout=1.0)
