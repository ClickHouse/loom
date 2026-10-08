"""The extraction's existing-memory context is fact rows only,
sized by the input, and the evidence write runs beside the pipeline."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tests.unit.test_extract_concurrent_projections import _input, _Log, _overlap, _wire


def _msgs(chars: int) -> list[dict[str, str]]:
    return [{"role": "user", "content": "x" * chars}]


def test_a_short_input_sees_the_short_limit_and_a_long_one_none(monkeypatch) -> None:
    from server.memory.app.handlers import existing_context_limit
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_existing_short_limit", 8)
    monkeypatch.setattr(settings, "memory_extract_compact_prompt_max_chars", 1200)
    assert existing_context_limit(_msgs(60), 50) == 8
    assert existing_context_limit(_msgs(1200), 50) == 8
    assert existing_context_limit(_msgs(1201), 50) == 0  # a long transcript gets no context
    assert existing_context_limit(_msgs(60), 5) == 5  # never above the caller's limit
    monkeypatch.setattr(settings, "memory_extract_existing_short_limit", 0)
    assert existing_context_limit(_msgs(60), 50) == 50  # 0 = off
    monkeypatch.setattr(settings, "memory_extract_existing_short_limit", 8)
    monkeypatch.setattr(settings, "memory_extract_compact_prompt_max_chars", 0)
    assert existing_context_limit(_msgs(60), 50) == 0  # no short tier → every input is long


@pytest.mark.asyncio
async def test_the_candidate_search_reads_fact_rows_only(monkeypatch) -> None:
    import server.memory.app.handlers as hm
    from server.memory import store as memory_repo
    from server.memory.app import retrieval as memory_search

    seen: list[Any] = []

    class _Emb:
        model_name = "fake"

        async def embed(self, text: str) -> list[float]:
            return [0.1, 0.2]

    async def _vs(q: Any) -> list[Any]:
        seen.append(q)
        return []

    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Emb())
    monkeypatch.setattr(memory_search, "vector_search", _vs)
    ident = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="-", session_id="s")
    await hm._fetch_relevant_existing_memories(ident, _msgs(60), limit=50)
    q = seen[0]
    assert q.execution_planes == ["fact"] and q.chunk_max_share == 0.0
    assert q.top_k == 8


@pytest.mark.asyncio
async def test_the_evidence_write_overlaps_the_pipeline(monkeypatch) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log)

    async def _slow_evidence(input: Any, msgs: Any, **kw: Any) -> list[str]:
        t0 = time.perf_counter()
        await asyncio.sleep(0.05)
        log.spans["evidence"] = (t0, time.perf_counter())
        return ["ev-1"]

    monkeypatch.setattr(mx, "_write_source_evidence", _slow_evidence)
    out = await mx.handle_memory_set_from_messages(_input())
    assert _overlap(log.spans["evidence"], log.spans["pipeline"])
    assert out.evidence_ids == ["ev-1"] and out.belief_source_links == 3


@pytest.mark.asyncio
async def test_a_failed_evidence_write_links_nothing_and_is_recorded(monkeypatch) -> None:
    import server.memory.app.extract.handler as mx

    log = _Log()
    _wire(monkeypatch, log)

    async def _boom(input: Any, msgs: Any, **kw: Any) -> list[str]:
        raise RuntimeError("evidence down")

    monkeypatch.setattr(mx, "_write_source_evidence", _boom)
    out = await mx.handle_memory_set_from_messages(_input())
    assert out.evidence_ids == [] and "memory_evidence" in log.failures
    assert [w.memory_key for w in out.written] == ["home"]


@pytest.mark.asyncio
async def test_a_long_transcript_runs_no_candidate_search(monkeypatch) -> None:
    """A long transcript gets no existing-memory context, so neither the embedding nor
    the vector search runs for it."""
    import server.memory.app.handlers as hm
    from server.memory import store as memory_repo
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_compact_prompt_max_chars", 1200)

    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("no candidate search for a long transcript")

    monkeypatch.setattr("server.embedding.get_embedder", boom)
    monkeypatch.setattr("server.memory.app.retrieval.vector_search", boom)
    ident = memory_repo.Identity(org="o", namespace="n", agent="a", user_id="u", session_id="s")
    assert await hm._fetch_relevant_existing_memories(ident, _msgs(5000), limit=50) == []
