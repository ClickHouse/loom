"""Background loops outside the supervisor tag their ClickHouse statements.

The workers child's boot reads and its tracker-registry loop, and the drops
buffer's timer flush, run in tasks no request or worker pass reaches, so each
sets its own query tag (`.claude/rules/clickhouse-sql.md`). CH-free: the calls
that would reach ClickHouse record the tag they see.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.ch import current_query_tag


@pytest.mark.asyncio
async def test_the_drops_timer_flush_is_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import drops

    seen: list[str] = []

    async def flush() -> int:
        seen.append(current_query_tag())
        return 0

    monkeypatch.setattr(drops, "flush_drops", flush)
    await drops._flush_and_rearm()
    assert seen == ["drops.flush"]


@pytest.mark.asyncio
async def test_the_workers_child_tags_its_boot_reads_and_tracker_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import boot, worker_main
    from server.code.app.index import repo_registry
    from server.core import model_work_gate
    from server.insights.app import provider_barrier, provider_runtime
    from server.tracker.app import registry_sources

    tags: list[str] = []
    second_refresh = asyncio.Event()

    async def refresh(*_: Any, **__: Any) -> None:
        tags.append(current_query_tag())
        if len(tags) == 2:
            second_refresh.set()

    async def noop(*_: Any, **__: Any) -> None:
        return None

    async def idle() -> None:
        await asyncio.Event().wait()

    async def start_refresher(_ch: Any) -> asyncio.Task[None]:
        return asyncio.create_task(idle())

    class _Emitter:
        async def start(self) -> None:
            return None

    class _Gate:
        closed = True

    monkeypatch.setattr(registry_sources, "refresh_db_sources", refresh)
    monkeypatch.setattr(provider_barrier, "register_process", noop)
    monkeypatch.setattr(provider_runtime, "load_active_configuration", noop)
    monkeypatch.setattr(provider_runtime, "refresh_loop", idle)
    monkeypatch.setattr("server.observability.get_emitter", lambda: _Emitter())
    monkeypatch.setattr(model_work_gate, "get_model_work_gate", lambda: _Gate())
    monkeypatch.setattr(repo_registry, "start_refresher", start_refresher)
    monkeypatch.setattr(boot, "start_runtime_overrides", lambda: asyncio.create_task(idle()))
    monkeypatch.setattr(boot, "await_warm_up", noop)
    monkeypatch.setattr(worker_main.settings, "tracker_sources_refresh_seconds", 0.0)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))

    tasks = await worker_main._start_services(object())
    try:
        await asyncio.wait_for(second_refresh.wait(), 5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert tags[:2] == ["workers_child.boot", "tracker.sources_refresh"]
    assert current_query_tag() == ""
