"""Supplemental reads refuse org waits and preserve successful phrase winners."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from server.core.errors import VectorAdmissionError
from server.core.vectors import admission
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search import supplemental
from server.memory.app.search.vector_legs import _search_query_for
from server.settings import settings
from tests.unit.test_memory_fanout_windows import _ctx, _hit


async def test_default_c16_sub_query_keeps_the_complete_original_success_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concurrent = 16
    from server.memory.app import retrieval

    ctx = _ctx()
    ctx.sub_queries = ["first", "second"]
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    monkeypatch.setattr(settings, "vector_scan_max_concurrent_per_org", concurrent)
    monkeypatch.setattr(settings, "vector_scan_max_waiters_per_org", 256)
    monkeypatch.setattr(settings, "ch_pool_maxsize", 64)
    monkeypatch.setattr(settings, "ch_pool_wait_ms", 5000)
    reads: set[asyncio.Task[Any]] = set()
    queries: list[Any] = []
    admitted: list[admission.VectorWork] = []
    active = 0
    peak = 0

    async def search(q):
        nonlocal active, peak
        task = asyncio.current_task()
        assert task is not None
        reads.add(task)
        queries.append(q)
        assert admission.current_work() is None
        with admission.vector_work(q.org, q.namespace):
            expected_work = admission.VectorWork(q.org, (q.namespace,), 1)
            assert admission.current_work() == expected_work
            async with admission.admit(pool_ready=True, wait_s=5.0):
                assert admission.current_work() == expected_work
                admitted.append(expected_work)
                active += 1
                peak = max(peak, active)
                try:
                    ticks = (100 if q.namespace == "ns0" else 0) if q.query_text == "first" else 60
                    for _ in range(ticks):
                        await asyncio.sleep(0)
                    result = [_hit(q.namespace, distance=0.2 if q.query_text == "first" else 0.4)]
                finally:
                    active -= 1
        assert admission.current_work() is None
        return result

    monkeypatch.setattr(retrieval, "vector_search", search)
    assert admission.current_work() is None
    request = asyncio.create_task(supplemental._run_sub_queries(ctx))
    try:
        await request
        expected_queries = [
            replace(_search_query_for(ctx, ns), query_vec=[0.5] * 4, top_k=5, query_text=phrase)
            for phrase in ctx.sub_queries
            for ns in ctx.namespaces
        ]
        assert queries == expected_queries
        assert admitted == [
            admission.VectorWork("o", (ns,), 1)
            for _phrase in ctx.sub_queries
            for ns in ctx.namespaces
        ]
        assert ctx.actual_work == {
            "embedding_calls": 2,
            "vector_queries": 18,
            "sub_query_count": 2,
        }
        assert len(reads) == 18 and all(task.done() for task in reads)
        assert 1 < peak <= concurrent
        assert active == 0 and gate.population == 0 and gate.orgs == {}
        assert admission.current_work() is None
        assert [h.namespace for h in ctx.all_hits] == ctx.namespaces
        assert [h.rank_score for h in ctx.all_hits] == pytest.approx([0.6] * 9)
        assert [h.model_dump() for h in ctx.all_hits] == [
            _MemorySearchHit(
                memory_key=ns,
                memory_type="semantic",
                content_excerpt=ns,
                distance=0.4,
                similarity=0.6,
                rank_score=0.6,
                namespace=ns,
            ).model_dump()
            for ns in ctx.namespaces
        ]
    finally:
        for task in (request, *reads):
            if not task.done():
                task.cancel()
        await asyncio.gather(request, *reads, return_exceptions=True)
        assert all(task.done() for task in (request, *reads))
        assert active == 0 and gate.population == 0 and gate.orgs == {}
        assert admission.current_work() is None


@pytest.mark.parametrize("owner", ["sub_queries", "set_expansion"])
async def test_supplemental_c2_refuses_complete_read_instead_of_waiting(
    monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    from server.memory.app import retrieval

    ctx = _ctx()
    ctx.namespaces = ["ns0", "ns1"]
    ctx.sub_queries = ["first", "second"]
    ctx.set_expansion_phrases = ["first", "second"]
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    monkeypatch.setattr(settings, "vector_scan_max_concurrent_per_org", 2)
    monkeypatch.setattr(settings, "vector_scan_max_waiters_per_org", 256)
    monkeypatch.setattr(settings, "ch_pool_maxsize", 64)
    monkeypatch.setattr(settings, "ch_pool_wait_ms", 5000)
    reads: set[asyncio.Task[Any]] = set()
    active = 0
    peak = 0
    completed = 0

    async def search(q):
        nonlocal active, peak, completed
        task = asyncio.current_task()
        assert task is not None
        reads.add(task)
        assert admission.current_work() is None
        with admission.vector_work(q.org, q.namespace):
            async with admission.admit(pool_ready=True, wait_s=5.0):
                active += 1
                peak = max(peak, active)
                try:
                    for _ in range(20):
                        await asyncio.sleep(0)
                    completed += 1
                    return [_hit(q.namespace)]
                finally:
                    active -= 1

    monkeypatch.setattr(retrieval, "vector_search", search)
    run = (
        supplemental._run_sub_queries if owner == "sub_queries" else supplemental._run_set_expansion
    )
    request = asyncio.create_task(run(ctx))
    try:
        with pytest.raises(VectorAdmissionError) as caught:
            await request
        assert caught.value.code == "VECTOR_CAPACITY_REFUSED"
        assert caught.value.extra == {"reason": "org_queue", "retry_after_s": 1}
        assert caught.value.status_code == 429
        assert caught.value.headers() == {"Retry-After": "1"}
        assert completed == 0
        assert peak == 2
        assert len(reads) == 4 and all(task.done() for task in reads)
        assert ctx.all_hits == []
        assert active == 0 and gate.population == 0 and gate.orgs == {}
        assert admission.current_work() is None
    finally:
        for task in (request, *reads):
            if not task.done():
                task.cancel()
        await asyncio.gather(request, *reads, return_exceptions=True)
        assert all(task.done() for task in (request, *reads))
        assert active == 0 and gate.population == 0 and gate.orgs == {}
        assert admission.current_work() is None


async def test_default_c16_public_client_keeps_the_complete_phrase_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concurrent = 16
    from types import SimpleNamespace

    from tests.unit.test_vector_read_admission import client

    ch, driver = client(monkeypatch)
    native_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []
    native_work: list[admission.VectorWork | None] = []

    async def native_query(sql: str, **kwargs: Any) -> Any:
        assert sql == "SELECT {ticks:UInt32} AS ticks"
        native_calls.append((kwargs["parameters"], kwargs["settings"]))
        native_work.append(admission.current_work())
        for _ in range(kwargs["parameters"]["ticks"]):
            await asyncio.sleep(0)
        return SimpleNamespace(result_rows=[], column_names=[], summary={})

    monkeypatch.setattr(driver, "query", native_query)
    from server.memory.app import retrieval

    ctx = _ctx()
    ctx.sub_queries = ["first", "second"]
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    monkeypatch.setattr(settings, "vector_scan_max_concurrent_per_org", concurrent)
    monkeypatch.setattr(settings, "vector_scan_max_waiters_per_org", 256)
    monkeypatch.setattr(settings, "ch_pool_maxsize", 64)
    monkeypatch.setattr(settings, "ch_pool_wait_ms", 5000)
    reads: set[asyncio.Task[Any]] = set()
    queries: list[Any] = []
    admitted: list[admission.VectorWork] = []
    active = 0
    peak = 0

    async def search(q):
        nonlocal active, peak
        task = asyncio.current_task()
        assert task is not None
        reads.add(task)
        queries.append(q)
        assert admission.current_work() is None
        with admission.vector_work(q.org, q.namespace):
            expected_work = admission.VectorWork(q.org, (q.namespace,), 1)
            assert admission.current_work() == expected_work
            admitted.append(expected_work)
            active += 1
            peak = max(peak, active)
            try:
                ticks = (100 if q.namespace == "ns0" else 0) if q.query_text == "first" else 60
                assert await ch.query_all("SELECT {ticks:UInt32} AS ticks", {"ticks": ticks}) == []
                result = [_hit(q.namespace, distance=0.2 if q.query_text == "first" else 0.4)]
            finally:
                active -= 1
        assert admission.current_work() is None
        return result

    monkeypatch.setattr(retrieval, "vector_search", search)
    assert admission.current_work() is None
    request = asyncio.create_task(supplemental._run_sub_queries(ctx))
    try:
        await request
        expected_queries = [
            replace(_search_query_for(ctx, ns), query_vec=[0.5] * 4, top_k=5, query_text=phrase)
            for phrase in ctx.sub_queries
            for ns in ctx.namespaces
        ]
        assert queries == expected_queries
        assert admitted == [
            admission.VectorWork("o", (ns,), 1)
            for _phrase in ctx.sub_queries
            for ns in ctx.namespaces
        ]
        assert ctx.actual_work == {
            "embedding_calls": 2,
            "vector_queries": 18,
            "sub_query_count": 2,
        }
        assert len(reads) == 18 and all(task.done() for task in reads)
        assert 1 < peak <= concurrent
        assert active == 0 and gate.population == 0 and gate.orgs == {}
        assert admission.current_work() is None
        assert [h.namespace for h in ctx.all_hits] == ctx.namespaces
        assert [h.rank_score for h in ctx.all_hits] == pytest.approx([0.6] * 9)
        assert [h.model_dump() for h in ctx.all_hits] == [
            _MemorySearchHit(
                memory_key=ns,
                memory_type="semantic",
                content_excerpt=ns,
                distance=0.4,
                similarity=0.6,
                rank_score=0.6,
                namespace=ns,
            ).model_dump()
            for ns in ctx.namespaces
        ]
    finally:
        for task in (request, *reads):
            if not task.done():
                task.cancel()
        await asyncio.gather(request, *reads, return_exceptions=True)
        assert all(task.done() for task in (request, *reads))
        assert active == 0 and gate.population == 0 and gate.orgs == {}
        assert admission.current_work() is None

    assert len(native_calls) == 18
    assert native_work == [admission.VectorWork(q.org, (q.namespace,), 1) for q in queries]
    for params, caps in native_calls:
        assert set(params) == {"ticks"}
        assert caps["max_rows_to_read"] == settings.vector_scan_max_rows_to_read
        assert caps["read_overflow_mode"] == "throw"
        assert 0 < caps["max_execution_time"] <= settings.ch_caller_shaped_max_execution_s
        assert 0 < caps["max_memory_usage"] <= settings.ch_caller_shaped_max_memory_bytes
    assert ch._slots is not None and ch._slots._value == 64
    assert ch._inflight == {} and ch._retired == {}
