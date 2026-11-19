"""A refused native read fails its owner; ordinary optional failures still fall back."""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import pg, request_work
from server.core.errors import (
    VectorAdmissionError,
    VectorCapacityError,
    VectorInputTooLargeError,
    VectorOverloadedError,
)
from server.core.vectors.admission import current_work
from server.knowledge.api.models_knowledge import KnowledgeSearchInput, _SearchHit
from server.knowledge.app import knowledge_search, source_passages, source_search_passes
from server.knowledge.app.knowledge_search import SearchRequest
from server.knowledge.app.search import ChunkHit, HybridSearchQuery
from server.knowledge.app.source_access import ReadPlan
from server.memory.api.models_lifecycle import MemoryStackInput
from server.memory.app import stack, stack_knowledge
from server.memory.app.ask import context_blocks, identifiers
from server.memory.app.ask.source_context import PreparedKnowledge
from server.memory.app.retrieval import derived
from server.settings import settings
from tests.unit.ch_fake import RecordingCH

BODY = "The complete ordinary passage stays intact.\nIts second line is retained too."
FACT = {
    "memory_key": "fact:known",
    "memory_type": "semantic",
    "content_excerpt": "The complete identifier fact cites 109741 and retains every word.",
    "created_at": "2025-01-01 12:00:00",
    "valid_at": "2025-01-01 12:00:00",
}
NS = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"
NEXT = "0b5f2a1c-7e3d-4f60-9a2b-1c2d3e4f5a6b"


def _failure(status: int, capacity: bool) -> Exception:
    if not capacity:
        return RuntimeError("ordinary optional read unavailable")
    if status == 429:
        return VectorAdmissionError("native row budget refused", reason="rows", retry_after_s=7)
    if status == 422:
        return VectorInputTooLargeError("complete vector input refused")
    assert status == 503
    return VectorOverloadedError("native pool refused", reason="pool", retry_after_s=7)


def _assert_refusal(actual: Exception, expected: Exception, status: int) -> None:
    assert actual is expected
    assert actual.status_code == status
    code = {429: "VECTOR_CAPACITY_REFUSED", 422: "VECTOR_INPUT_TOO_LARGE", 503: "OVERLOADED"}
    detail = {"code": code[status], "message": str(expected)}
    if status != 422:
        detail.update(reason="rows" if status == 429 else "pool", retry_after_s=7)
    assert actual.to_detail() == detail
    assert actual.headers() == (None if status == 422 else {"Retry-After": "7"})


def _chunk_row(*, excluded: bool = False) -> dict[str, Any]:
    return {
        "namespace": "docs",
        "source_uri": "ordinary:excluded" if excluded else "ordinary:complete",
        "chunk_idx": 1 if excluded else 0,
        "doc_version": 10,
        "content": "Another bot's complete rule." if excluded else BODY,
        "context": "stored context",
        "tags": ["source-kind:assistant_turn", "agent:other"] if excluded else ["doc"],
        "chunk_metadata": "{}",
        "distance": 0.3 if excluded else 0.2,
        "agent": "-",
        "session_id": "-",
        "source_class": "",
    }


def _chunk_hit() -> ChunkHit:
    return ChunkHit(
        "docs",
        "ordinary:complete",
        0,
        BODY,
        ["doc"],
        0.2,
        doc_version=10,
        context="stored context",
        score=0.8,
    )


def _passage_hit() -> _SearchHit:
    return _SearchHit(
        namespace="docs",
        source_uri="ordinary:complete",
        chunk_idx=0,
        content=BODY,
        tags=["doc"],
        distance=0.2,
        similarity=0.8,
        score=0.8,
    )


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch):
    """Use real handler/store code; substitute only CH and the embedder."""
    token = request_work._current.set(None)
    request_work.begin()
    state = SimpleNamespace(site="", failure=None, refused=[], vector_widths=[])

    class Embedder:
        model_name = "fake"

        async def embed(self, _text: str) -> list[float]:
            return [1.0, 0.0]

    def answer(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        is_fact = "FROM loom.memory_embeddings" in sql and "cosineDistance" in sql
        is_chunk = "FROM loom.memory_chunks" in sql and "cosineDistance" in sql
        is_knowledge = "FROM loom.chunks" in sql and "cosineDistance" in sql
        is_mirror = "FORMAT JSONEachRow" in sql
        if is_knowledge and not is_mirror:
            state.vector_widths.append(params["top_k"])
        refusal = (
            (state.site in {"fact", "chase"} and is_fact)
            or (state.site in {"chunk", "mention", "decisive"} and is_chunk)
            or (state.site == "mirror" and is_mirror)
            or (state.site in {"ask-first", "stack-first"} and is_knowledge)
            or (
                state.site in {"passages-wide", "stack-wide", "lazy-wide"}
                and is_knowledge
                and params["top_k"] > 2
            )
        )
        if refusal:
            work = current_work()
            assert work is not None and work.org == "acme" and work.weight == 1
            assert work.namespaces == ((NS,) if is_mirror else ("docs",))
            state.refused.append((sql, dict(params)))
            raise state.failure
        if is_fact:
            return [FACT]
        if is_chunk:
            return []
        if is_knowledge:
            rows = [_chunk_row()]
            return rows + [_chunk_row(excluded=True)] if state.site == "lazy-wide" else rows
        if "FROM loom.memory FINAL" in sql:
            return []  # The real profile, pinned and recent sections have no corpus rows.
        raise AssertionError(f"unrouted native read: {' '.join(sql.split())[:200]}")

    ch = RecordingCH().on(re.compile(r".*", re.DOTALL), answer)
    ch.install(monkeypatch, "server.memory.app.stack", "server.knowledge.store.source_candidates")
    monkeypatch.setattr("server.embedding.get_embedder", Embedder)
    monkeypatch.setattr(knowledge_search, "get_embedder", Embedder)
    monkeypatch.setattr(pg, "enabled", lambda: False)
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", False)
    monkeypatch.setattr(settings, "knowledge_noise_filter", False)
    monkeypatch.setattr(settings, "memory_ask_identifier_inject_chunks", 4)
    monkeypatch.setattr(settings, "memory_ask_decisive_chunks", 4)
    monkeypatch.setattr(settings, "memory_ask_mention_window_chunks", 4)
    state.ch = ch
    try:
        yield state
    finally:
        request_work._current.reset(token)


@pytest.mark.parametrize(
    "site,status",
    [("fact", 429), ("chunk", 422), ("mention", 503), ("decisive", 429), ("chase", 503)],
    ids=["fact-429", "chunk-422", "mention-503", "decisive-429", "chase-503"],
)
@pytest.mark.parametrize("capacity", [True, False], ids=["refusal", "ordinary-fallback"])
async def test_identifier_owners_keep_complete_read_refusals(native, site, status, capacity):
    native.site, native.failure = site, _failure(status, capacity)
    question = "What do we know about 109741?"
    if site == "mention":
        question = "We talked about a movie last week. Which one?"
    elif site == "decisive":
        question = "What caused the bug in 109741?"
    request = SimpleNamespace(
        org="acme",
        namespace="docs",
        agent="reader",
        user_id="-",
        scope="private",
        observation_date="2025-01-20",
        query=question,
    )
    if site == "mention":
        call = identifiers._mention_window_chunks(request, question, [])
    elif site == "decisive":
        call = identifiers._read_decisive_chunks(request, ["109741"], [1.0, 0.0], False, settings)
    elif site == "chase":
        frontier = [SimpleNamespace(memory_key="start", content_excerpt=f"Value: {NEXT}")]
        call = identifiers._chase_identifiers(
            request,
            seen=["109741"],
            frontier=frontier,
            ranked=frontier,
            hops=2,
            cap=8,
        )
    else:
        call = identifiers._inject_identifier_memories(request, ["109741"], [])
    try:
        if capacity:
            with pytest.raises(VectorCapacityError) as caught:
                await asyncio.wait_for(call, timeout=1)
            _assert_refusal(caught.value, native.failure, status)
        else:
            result = await asyncio.wait_for(call, timeout=1)
            if site == "chunk":
                assert len(result) == 1
                hit = result[0]
                assert (
                    hit.memory_key,
                    hit.content_excerpt,
                    hit.rank_score,
                    hit.similarity,
                    hit.distance,
                    hit.source,
                    hit.valid_at,
                ) == (
                    "fact:known",
                    FACT["content_excerpt"],
                    1.0,
                    1.0,
                    0.0,
                    "fact",
                    "2025-01-01 12:00:00",
                )
            else:
                assert result == (([], 1) if site == "chase" else [])
    finally:
        assert len(native.refused) == 1
        _sql, params = native.refused[0]
        assert params["qv"] == [1.0, 0.0]
        if site == "decisive":
            assert "root cause" in params["marks"]
        elif site == "mention":
            assert params["d0"] < params["d1"]
        elif site == "chase":
            assert params["id0"] == NEXT


@pytest.mark.parametrize(
    "site,status",
    [
        ("mirror", 429),
        ("ask-first", 422),
        ("passages-wide", 503),
        ("stack-first", 503),
        ("stack-wide", 422),
        ("lazy-wide", 429),
    ],
    ids=[
        "mirror-429",
        "ask-first-422",
        "passages-wide-503",
        "stack-first-503",
        "stack-wide-422",
        "lazy-wide-429",
    ],
)
@pytest.mark.parametrize("capacity", [True, False], ids=["refusal", "ordinary-fallback"])
async def test_document_owners_keep_complete_read_refusals(native, site, status, capacity):
    native.site, native.failure = site, _failure(status, capacity)
    plan = ReadPlan("acme", marked=site != "lazy-wide")
    request = SearchRequest(
        KnowledgeSearchInput(
            org="acme",
            namespace="docs",
            query="question",
            top_k=2,
            search_mode="semantic",
            namespace_scope="this",
            rerank="none",
            collapse_sections=False,
            narrow_spans=False,
        ),
        ("docs",),
        (),
    )
    if site == "mirror":
        plan._bindings = ((NS, SID, "docs"),)
        passes = source_search_passes.SearchPasses(plan)
        ordinary = _chunk_hit()

        async def collect_pass():
            await passes.collect(
                HybridSearchQuery("acme", ("docs",), "fake", [1.0, 0.0], 2),
                {"semantic": [ordinary]},
                legs=("semantic",),
                fallback=[ordinary],
                top_k=2,
            )
            return await passes.finish()

        call = collect_pass()
    elif site == "ask-first":
        call = context_blocks.knowledge_passages(
            SimpleNamespace(agent="reader"),
            "question",
            prepared=PreparedKnowledge(request, plan),
        )
    elif site == "passages-wide":
        call = source_passages.collect(request, plan, "reader")
    elif site == "stack-first":
        call = stack.run_checked_stack(
            MemoryStackInput(
                org="acme",
                namespace="docs",
                agent="reader",
                seed_query="question",
                include_knowledge=True,
                knowledge_top_k=2,
                procedure_limit=0,
                pinned_limit=0,
                recent_limit=0,
            ),
            plan,
        )
    else:
        call = stack_knowledge.collect("acme", "docs", "reader", "question", 2, plan)
    try:
        if capacity:
            with pytest.raises(VectorCapacityError) as caught:
                await asyncio.wait_for(call, timeout=1)
            _assert_refusal(caught.value, native.failure, status)
        else:
            result = await asyncio.wait_for(call, timeout=1)
            if site == "mirror":
                assert [[asdict(hit.chunk) for hit in pool] for pool in result] == [
                    [asdict(_chunk_hit())]
                ]
                assert not result[0][0].mirrored
                assert plan.failed
            elif site == "ask-first":
                assert result.passages is None and await result.current() == ("", 0)
            elif site == "passages-wide":
                assert [hit.model_dump() for hit in await result.current()] == [
                    _passage_hit().model_dump()
                ]
            elif site == "stack-first":
                assert (await result.current()).model_dump() == {
                    "entries": [],
                    "tokens_used": 0,
                    "tokens_budget": 4000,
                    "truncated": False,
                    "section_counts": {
                        name: 0
                        for name in ("profile", "pinned", "recent_recall", "procedure", "knowledge")
                    },
                }
            else:
                assert [asdict(hit) for hit in await result.current()] == [asdict(_chunk_hit())]
    finally:
        assert len(native.refused) == 1
        if site in {"passages-wide", "stack-wide", "lazy-wide"}:
            assert native.vector_widths == [2, 8]
        elif site in {"ask-first", "stack-first"}:
            assert native.vector_widths == [2]


def _temporal_call(ch):
    return derived.fetch_temporal_diff(
        ch,
        org="acme",
        namespace="docs",
        agent="reader",
        user_id="-",
        operand_vecs=[[0.0], [1.0]],
        embedding_model="fake",
    )


@pytest.mark.parametrize(
    "failed_operand,status", [(0, 429), (1, 503)], ids=["first-429", "second-503"]
)
async def test_temporal_refusal_joins_the_other_native_operand(failed_operand, status):
    sibling_started, sibling_settled = asyncio.Event(), asyncio.Event()
    failure = _failure(status, True)
    native_tasks = set()
    diff_calls = []

    class CH:
        async def query_all(self, sql, params=None):
            if "dateDiff" in sql:
                diff_calls.append(params)
                return [{"days": 1}]
            assert "cosineDistance" in sql
            native_tasks.add(asyncio.current_task())
            if params["q"] == [float(failed_operand)]:
                await sibling_started.wait()
                raise failure
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                sibling_settled.set()

    caller = asyncio.create_task(_temporal_call(CH()))
    try:
        with pytest.raises(VectorCapacityError) as caught:
            await asyncio.wait_for(asyncio.shield(caller), timeout=1)
        _assert_refusal(caught.value, failure, status)
        assert sibling_started.is_set()
        assert sibling_settled.is_set(), (
            "the owning handler returned while its native sibling was live"
        )
        assert all(task.done() for task in native_tasks)
        assert diff_calls == []
    finally:
        for task in native_tasks | {caller}:
            task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*native_tasks, caller, return_exceptions=True), timeout=1
        )


async def test_temporal_first_refusal_survives_repeated_cancellation_while_joining():
    started, cleanup_started, release, settled = (asyncio.Event() for _ in range(4))
    failure = _failure(422, True)
    native_tasks = set()

    class CH:
        async def query_all(self, sql, params=None):
            assert "cosineDistance" in sql
            native_tasks.add(asyncio.current_task())
            if params["q"] == [0.0]:
                await started.wait()
                raise failure
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release.wait()
                settled.set()

    caller = asyncio.create_task(_temporal_call(CH()))
    cleanup_wait = asyncio.create_task(cleanup_started.wait())
    try:
        done, _ = await asyncio.wait(
            {caller, cleanup_wait}, timeout=1, return_when=asyncio.FIRST_COMPLETED
        )
        assert done, "the operand pair made no progress"
        if caller.done():
            assert caller.exception() is failure
        assert cleanup_started.is_set(), "first refusal returned without joining its sibling"
        assert not caller.done() and not settled.is_set()
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done() and not settled.is_set()
        release.set()
        with pytest.raises(VectorInputTooLargeError) as caught:
            await asyncio.wait_for(asyncio.shield(caller), timeout=1)
        _assert_refusal(caught.value, failure, 422)
        assert settled.is_set() and all(task.done() for task in native_tasks)
    finally:
        release.set()
        for task in native_tasks | {caller, cleanup_wait}:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(
            asyncio.gather(*native_tasks, caller, cleanup_wait, return_exceptions=True),
            timeout=1,
        )


async def test_temporal_two_native_operands_keep_the_complete_interval_result():
    calls = []

    class CH:
        async def query_all(self, sql, params=None):
            calls.append((sql, dict(params or {})))
            if "dateDiff" in sql:
                assert params == {"a": date(2025, 1, 2), "b": date(2025, 1, 5)}
                return [{"days": 3}]
            assert "cosineDistance" in sql
            first = params["q"] == [0.0]
            return [
                {
                    "event_date": date(2025, 1, 2 if first else 5),
                    "distance": 0.2,
                    "excerpt": "Complete first event." if first else "Complete second event.",
                    "memory_key": "first" if first else "second",
                    "anchored": True,
                }
            ]

    result = await asyncio.wait_for(_temporal_call(CH()), timeout=1)
    assert result == {
        "interval_days": 3,
        "from_date": "2025-01-02",
        "to_date": "2025-01-05",
        "from_excerpt": "Complete first event.",
        "to_excerpt": "Complete second event.",
        "to_observation": False,
        "from_event_dated": True,
        "to_event_dated": True,
        "all_event_dated": True,
    }
    assert len(calls) == 3
