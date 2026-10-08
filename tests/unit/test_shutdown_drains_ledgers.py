"""Shutdown waits for the policy decision rows and the memory side effects
(exposure rows, recall credit, verify mirror, ask credit) still being written, and flushes the drop ledger after the side effects that record
drops, before the ClickHouse client goes."""

from __future__ import annotations

import asyncio
import inspect

import pytest

import server.boot as boot
import server.main as m
from server.identity.app import policy_gate
from server.memory.app import after_write


@pytest.mark.asyncio
@pytest.mark.parametrize("mod,tasks", [(policy_gate, "_LEDGER_TASKS"), (after_write, "_TASKS")])
async def test_drain_waits_for_the_rows_in_flight(mod, tasks) -> None:
    landed: list[int] = []

    async def _write() -> None:
        await asyncio.sleep(0.01)
        landed.append(1)

    held = getattr(mod, tasks)
    if hasattr(held, "spawn"):
        held.spawn(_write(), name="probe")
    else:
        t = asyncio.get_running_loop().create_task(_write())
        held.add(t)
        t.add_done_callback(held.discard)
    assert await mod.drain(timeout_s=2.0) == 1
    assert landed == [1]
    assert await mod.drain(timeout_s=2.0) == 0


def test_the_lifespan_drains_them_before_the_client_disconnects() -> None:
    src = inspect.getsource(boot.drain_writes)
    order = [
        src.index("_policy_gate.drain("),
        src.index("_after_write.drain("),
        src.index("drops.flush_drops()"),
    ]
    assert order == sorted(order)
    life = inspect.getsource(m.lifespan)
    assert life.index("await boot.drain_writes()") < life.index("await ch.disconnect()")


def test_the_writes_drain_before_the_emitter_and_the_http_pool_stop() -> None:
    """A deferred conflict judge still in flight at shutdown calls the LLM over
    the shared HTTP pool and emits its settle row through the event emitter:
    drained after either stops, the call fails on a closed pool and the row
    sits in a queue nothing flushes."""
    life = inspect.getsource(m.lifespan)
    drain = life.index("await boot.drain_writes()")
    assert life.index("supervisor.stop_all(") < drain
    assert drain < life.index("await emitter.stop()")
    assert drain < life.index("await _http_clients.aclose()")


@pytest.mark.asyncio
async def test_an_exposure_row_is_an_after_write_task_the_shutdown_drain_lands(
    monkeypatch,
) -> None:
    from server.memory.app import exposure_ledger
    from server.settings import settings

    inserted: list[str] = []

    class _CH:
        async def insert(self, table: str, rows: list, **_: object) -> None:
            await asyncio.sleep(0.01)
            inserted.append(table)

    monkeypatch.setattr(settings, "memory_exposure_ledger_enabled", True)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    row = exposure_ledger.record_exposure(
        operation="memory.search", org="o", namespace="n", keys=["k"]
    )
    assert row is not None and after_write.pending() == 1
    assert await after_write.drain(timeout_s=2.0) == 1
    assert inserted == [exposure_ledger.TABLE]
