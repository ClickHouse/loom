"""A memory.search pass that fails or is cancelled leaves none of its launched reads running."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import handler as search_handler
from server.memory.app.search import scope


def _input() -> MemorySearchInput:
    return MemorySearchInput(
        org="o", namespace="n", agent="a", query="q", namespace_scope="all_mine"
    )


async def _forever(*_a: Any, **_k: Any) -> Any:
    await asyncio.sleep(3600)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> list[scope._SearchRun]:
    """Scope reads that never finish, and the `_SearchRun` each pass launched."""
    import server.memory.app.embedder_calibration as calibration
    import server.memory.app.rerank as rerank

    monkeypatch.setattr(calibration, "resolve_calibration", _forever)
    monkeypatch.setattr(rerank, "get_namespace_rerank_config", _forever)
    monkeypatch.setattr(scope, "resolve_fanout", _forever)

    async def _resolve(inp: MemorySearchInput) -> Any:
        return inp, False, 0, None

    async def _no_reread(_inp: MemorySearchInput) -> None:
        return None

    async def _work(_inp: MemorySearchInput) -> Any:
        return {}, None

    runs: list[scope._SearchRun] = []

    def _launch(inp: MemorySearchInput) -> scope._SearchRun:
        run = scope._launch_scope_reads(inp)
        runs.append(run)
        return run

    monkeypatch.setattr(search_handler, "_resolve_search_input", _resolve)
    monkeypatch.setattr(search_handler, "_fresh_namespace_reread", _no_reread)
    monkeypatch.setattr(search_handler, "_start_actual_work", _work)
    monkeypatch.setattr(search_handler, "get_embedder", lambda: None)
    monkeypatch.setattr(search_handler, "_launch_scope_reads", _launch)
    return runs


def _tasks(run: scope._SearchRun) -> list[asyncio.Task[Any]]:
    return [t for t in (run.rerank_cfg_task, run.calibration_task, run.fanout_task) if t]


async def test_a_failing_stage_cancels_the_scope_reads(
    launched: list[scope._SearchRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("enrichment failed")

    monkeypatch.setattr(search_handler, "_resolve_query_enrichment", _boom)
    with pytest.raises(RuntimeError):
        await search_handler.handle_memory_search(_input())
    (run,) = launched
    tasks = _tasks(run)
    assert len(tasks) == 3
    await asyncio.sleep(0)
    assert all(t.cancelled() for t in tasks)


async def test_a_timed_out_pass_cancels_the_scope_reads(
    launched: list[scope._SearchRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_handler, "_resolve_query_enrichment", _forever)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(search_handler.handle_memory_search(_input()), timeout=0.05)
    (run,) = launched
    await asyncio.sleep(0)
    assert all(t.cancelled() for t in _tasks(run))


async def test_cancel_pending_leaves_finished_tasks_alone() -> None:
    async def _value() -> int:
        return 1

    done = asyncio.ensure_future(_value())
    await done
    pending = asyncio.ensure_future(_forever())
    run = scope._SearchRun(
        rerank_override="",
        rerank_cfg_task=None,
        calibration_task=done,
        fanout_task=None,
        side_tasks=[None, pending],
    )
    run.cancel_pending()
    await asyncio.sleep(0)
    assert done.result() == 1
    assert pending.cancelled()


async def test_refusal_joins_scope_reads_before_escaping(
    launched: list[scope._SearchRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import VectorInputTooLargeError

    started, finished = asyncio.Event(), asyncio.Event()
    refusal = VectorInputTooLargeError("summary input exceeds 256 names")

    async def calibration(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        await started.wait()
        raise refusal

    monkeypatch.setattr("server.memory.app.embedder_calibration.resolve_calibration", calibration)
    monkeypatch.setattr(search_handler, "_resolve_query_enrichment", fail)
    try:
        with pytest.raises(VectorInputTooLargeError) as raised:
            await search_handler.handle_memory_search(_input())
        assert raised.value is refusal
        assert finished.is_set()
        assert all(t.done() for t in _tasks(launched[0]))
    finally:
        for run in launched:
            await asyncio.gather(*_tasks(run), return_exceptions=True)


async def test_repeated_cancel_during_cleanup_preserves_the_first_typed_refusal(
    launched: list[scope._SearchRun], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import VectorInputTooLargeError

    started, cleaning, release, finished = (asyncio.Event() for _ in range(4))
    refusal = VectorInputTooLargeError("summary input exceeds 256 names")

    async def calibration(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        await started.wait()
        raise refusal

    monkeypatch.setattr("server.memory.app.embedder_calibration.resolve_calibration", calibration)
    monkeypatch.setattr(search_handler, "_resolve_query_enrichment", fail)
    request = asyncio.create_task(search_handler.handle_memory_search(_input()))
    try:
        async with asyncio.timeout(2):
            await cleaning.wait()
            assert request.cancel()
            await asyncio.sleep(0)
            assert request.cancel()
            release.set()
            with pytest.raises(VectorInputTooLargeError) as raised:
                await request
        assert raised.value is refusal
        assert finished.is_set() and all(t.done() for t in _tasks(launched[0]))
    finally:
        release.set()
        await asyncio.gather(
            request, *(_tasks(launched[0]) if launched else []), return_exceptions=True
        )


async def test_successful_search_inside_except_propagates_cleanup_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.domain.search_types import SearchHit
    from tests.unit.memory_search_fakes import install_search_fakes

    state = install_search_fakes(monkeypatch)
    state.hits_by_ns["n"] = [
        SearchHit(
            memory_key="color",
            memory_type="semantic",
            content_excerpt="favorite color is blue",
            distance=0.1,
            rank_score=0.9,
        )
    ]
    started, cleaning, release, finished = (asyncio.Event() for _ in range(4))
    runs: list[scope._SearchRun] = []
    children: list[asyncio.Task[Any]] = []
    completed: list[Any] = []
    launch = search_handler._launch_scope_reads
    finish = search_handler._finish_search_output

    async def owned() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    def record_run(inp: MemorySearchInput) -> scope._SearchRun:
        run = launch(inp)
        runs.append(run)
        return run

    async def finish_with_owned_child(ctx: Any, output: Any, hits: Any) -> None:
        await finish(ctx, output, hits)
        completed.append(output)
        child = asyncio.create_task(owned())
        children.append(child)
        runs[-1].side_tasks.append(child)
        await started.wait()

    async def request_from_except() -> Any:
        try:
            raise LookupError("unrelated caller exception")
        except LookupError:
            return await search_handler.handle_memory_search(
                MemorySearchInput(
                    org="o",
                    namespace="n",
                    agent="a",
                    query="favorite color",
                    recipe="fast",
                    auto_touch=False,
                )
            )

    monkeypatch.setattr(search_handler, "_launch_scope_reads", record_run)
    monkeypatch.setattr(search_handler, "_finish_search_output", finish_with_owned_child)
    request = asyncio.create_task(request_from_except())
    try:
        async with asyncio.timeout(2):
            await cleaning.wait()
            assert completed and children and not children[0].done()
            assert request.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert finished.is_set() and all(t.done() for t in children)
        assert all(t.done() for run in runs for t in _tasks(run))
    finally:
        release.set()
        if not request.done():
            request.cancel()
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(request, *children, return_exceptions=True)
