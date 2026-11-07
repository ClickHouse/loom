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
        src.index("_graph_marks.drain("),
        src.index("drops.flush_drops()"),
    ]
    assert order == sorted(order)
    tail = inspect.getsource(m._shut_down)
    assert tail.index("_shutdown_drains(") < tail.index("await boot.leave_deployment(")
    assert "await boot.drain_writes()" in inspect.getsource(m._shutdown_drains)


def test_the_writes_drain_before_the_emitter_and_the_http_pool_stop() -> None:
    """A deferred conflict judge still in flight at shutdown calls the LLM over
    the shared HTTP pool and emits its settle row through the event emitter:
    drained after either stops, the call fails on a closed pool and the row
    sits in a queue nothing flushes."""
    life = inspect.getsource(m._shutdown_drains)
    drain = life.index("await boot.drain_writes()")
    assert life.index("_leader.shutdown(lease, supervisor)") < drain
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


@pytest.mark.asyncio
async def test_a_claim_past_its_wait_leaves_the_emitter_and_the_clients_to_close(
    monkeypatch,
) -> None:
    # An agent claim's drain is bounded: one still running past it is left to
    # run on, and the emitter and the HTTP clients still close.
    from server.clients import _http
    from server.identity.app import agent_claim
    from server.observability import langfuse_scores, retrieval_judge
    from server.transport import mcp_runs
    from server.workers import leader

    closed: list[str] = []

    async def nothing(*_: object, **__: object) -> None:
        return None

    async def aclose() -> None:
        closed.append("http")

    class _Emitter:
        async def stop(self) -> None:
            closed.append("emitter")

    for target, name in [
        (leader, "shutdown"),
        (mcp_runs, "drain_ledger_writes"),
        (boot, "drain_writes"),
        (retrieval_judge, "stop"),
        (langfuse_scores, "drain"),
    ]:
        monkeypatch.setattr(target, name, nothing)
    monkeypatch.setattr(_http, "aclose", aclose)
    monkeypatch.setattr(m._otel, "shutdown", lambda: None)
    monkeypatch.setattr(agent_claim, "SHUTDOWN_WAIT_S", 0.05)
    claim = agent_claim._running.spawn(asyncio.sleep(10), name="probe")
    try:
        await m._shutdown_drains(None, None, _Emitter())
        assert closed == ["emitter", "http"] and not claim.done()
    finally:
        claim.cancel()
