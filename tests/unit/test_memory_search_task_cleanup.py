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
    import server.auth.context as auth_context
    import server.memory.app.embedder_calibration as calibration
    import server.memory.app.rerank as rerank

    monkeypatch.setattr(calibration, "resolve_calibration", _forever)
    monkeypatch.setattr(rerank, "get_namespace_rerank_config", _forever)
    monkeypatch.setattr(auth_context, "resolve_fanout", _forever)

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
    return [t for t in (run.rerank_cfg_task, run.calibration_task, run.my_namespaces_task) if t]


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
        my_namespaces_task=None,
        order_authoritative=False,
        side_tasks=[None, pending],
    )
    run.cancel_pending()
    await asyncio.sleep(0)
    assert done.result() == 1
    assert pending.cancelled()
