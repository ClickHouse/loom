"""A search's writes leave the read path. The MCP session run
is registered at once under `mcp-<session_id>` while `run.begin` lands in the
background; closing waits for the begin so complete never precedes it; the
page park is a fire-and-forget insert whose key is known before the row."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.memory.app import search_pages as sp
from server.transport import mcp_runs

DEFAULTS = {"org": "clickhouse", "namespace": "ns-a", "agent": "cursor"}


def rid(session_id: str) -> str:
    """The implicit run id of `session_id` under DEFAULTS' org, for an unpinned caller."""
    return mcp_runs.run_id_for("clickhouse", "-", session_id)


@pytest.fixture(autouse=True)
def _flag(monkeypatch: pytest.MonkeyPatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "mcp_session_run_idle_seconds", 1800, raising=False)
    mcp_runs._RUNS.clear()
    mcp_runs._BEGIN_FAILED_AT.clear()
    yield
    mcp_runs._RUNS.clear()
    mcp_runs._BEGIN_FAILED_AT.clear()


def test_ensure_returns_before_run_begin_lands_and_close_waits_for_it(monkeypatch) -> None:
    import server.tasks.app.run as h

    gate = asyncio.Event()
    order: list[str] = []

    async def slow_begin(inp: Any) -> bool:
        await gate.wait()
        order.append(f"begin:{inp.run_id}")
        return True

    async def complete(inp: Any) -> Any:
        order.append(f"complete:{inp.run_id}")
        from server.tasks.api.models_run import RunCompleteOutput

        return RunCompleteOutput(run_id=inp.run_id, status=inp.status, duration_ms=inp.duration_ms)

    monkeypatch.setattr(h, "begin_session_run", slow_begin)
    monkeypatch.setattr(h, "handle_run_complete", complete)
    monkeypatch.setattr(h, "active_session_run", AsyncMock(return_value=None))
    monkeypatch.setattr(h, "session_run_calls", AsyncMock(return_value=("ns-a", 1, None)))

    async def _run() -> None:
        run = await asyncio.wait_for(mcp_runs.ensure("sess-9", DEFAULTS, goal="g", now=1.0), 0.5)
        assert run is not None and run.run_id == rid("sess-9"), "the call has its id at once"
        assert order == [], "run.begin has not landed and the call did not wait for it"
        closer = asyncio.create_task(mcp_runs.close_session("sess-9"))
        await asyncio.sleep(0)
        assert order == [], "close waits for the begin"
        gate.set()
        assert await closer is True
        assert order == [f"begin:{rid('sess-9')}", f"complete:{rid('sess-9')}"]

    asyncio.run(_run())


def test_ensure_never_awaits_run_begin_inline() -> None:
    src = inspect.getsource(mcp_runs.ensure)
    assert "await handle_run_begin" not in src and "_PENDING.spawn(" in src


def test_page_park_is_fire_and_forget_and_names_the_flush_window() -> None:
    assert sp.PARK_INSERT_SETTINGS == {"async_insert": 1, "wait_for_async_insert": 0}
    assert "settings=PARK_INSERT_SETTINGS" in inspect.getsource(sp.store_page)
    from server.memory.app.search.output import _serve_parked_page

    assert "not yet flushed" in inspect.getsource(_serve_parked_page)
