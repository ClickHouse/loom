"""The worker child owns a cell-only producer before it starts supervised work."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server import boot, worker_main
from server.core import ch as ch_module
from server.core import instances, retirement
from server.insights.app import provider_barrier
from server.memory.app import embedder_calibration
from server.settings import settings
from server.workers import child, leader


@pytest.fixture
async def worker_boot(monkeypatch):
    events = []
    tasks = []
    producers = []
    expected = RuntimeError("first cell write failed")
    fail = [False]
    first_error = [expected]
    cleanup_errors = {}
    gate = retirement.get_gate()

    async def state_loop():
        try:
            await asyncio.Event().wait()  # _serve owns and gathers this cell-state service.
        finally:
            events.append("state stopped")

    async def state():
        events.append("cell state")
        task = asyncio.create_task(state_loop())
        tasks.append(task)
        return [task]

    async def enroll():
        gate.enrolled = True

    async def cell_beat(_ch):
        events.append("cell beat")
        if fail[0]:
            raise first_error[0]

    async def services(_ch):
        events.append("services")
        return []

    async def drain():
        events.append("drain")

    async def leave(_ch, **_kwargs):
        events.append("retire")
        assert all(task.done() for task in producers)
        if "leave" in cleanup_errors:
            raise cleanup_errors["leave"]

    async def close():
        events.append("close")
        if "close" in cleanup_errors:
            raise cleanup_errors["close"]

    class Thread:
        def __init__(self, *, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            if self.target is child.read_deadlines:
                self.args[2]()  # Parent EOF requests normal shutdown without a real thread.

    class Supervisor:
        def start_all(self):
            events.append("supervisor")
            producers.extend(gate.producers)

        async def stop_all(self, **_kwargs):
            events.append("supervisor stopped")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_args: None)
    monkeypatch.setattr(worker_main.threading, "Thread", Thread)
    monkeypatch.setattr(settings, "cell", "eu-2")
    monkeypatch.setattr(settings, "serves_traffic", True)
    monkeypatch.setattr(settings, "instance_heartbeat_seconds", 10.0)
    monkeypatch.setattr(leader, "_CURRENT", None)
    ch = SimpleNamespace(connect=AsyncMock())
    monkeypatch.setattr(ch_module, "get_client", lambda: ch)
    monkeypatch.setattr(boot, "open_storage_authority", AsyncMock())
    monkeypatch.setattr(boot, "close_storage_authority", close)
    monkeypatch.setattr(boot, "start_cell_state", state)
    monkeypatch.setattr(boot, "leave_deployment", leave)
    monkeypatch.setattr(provider_barrier, "register_process", enroll)
    monkeypatch.setattr(instances, "cell_beat", cell_beat)
    monkeypatch.setattr(worker_main, "_start_services", services)
    monkeypatch.setattr(worker_main, "get_supervisor", Supervisor)
    monkeypatch.setattr(worker_main, "_drain", drain)
    monkeypatch.setattr(child, "beat", AsyncMock())
    monkeypatch.setattr(embedder_calibration, "stop_refresh_loop", lambda: None)
    return SimpleNamespace(
        events=events,
        tasks=tasks,
        producers=producers,
        expected=expected,
        fail=fail,
        first_error=first_error,
        cleanup_errors=cleanup_errors,
        gate=gate,
    )


@pytest.mark.asyncio
async def test_worker_acknowledges_own_cell_before_work_and_stops_it_before_retirement(worker_boot):
    value = worker_boot
    try:
        async with asyncio.timeout(1.0):
            await worker_main._serve(-1, -1)
        assert "cell beat" in value.events
        assert value.events.index("cell state") < value.events.index("cell beat")
        assert value.events.index("cell beat") < value.events.index("services")
        assert value.events.index("services") < value.events.index("supervisor")
        assert value.producers and all(task.done() for task in value.producers)
        assert all(task.done() for task in value.tasks)
        assert value.events.index("drain") < value.events.index("retire")
        assert value.gate.producers_stopped is True
        assert settings.serves_traffic is True
    finally:
        for task in value.tasks + value.producers:
            task.cancel()
        await asyncio.gather(*value.tasks, *value.producers, return_exceptions=True)


@pytest.mark.asyncio
async def test_first_worker_cell_write_failure_retains_cause_and_never_starts_work(worker_boot):
    value = worker_boot
    value.fail[0] = True
    try:
        async with asyncio.timeout(1.0):
            with pytest.raises(RuntimeError) as caught:
                await worker_main._serve(-1, -1)
        assert caught.value is value.expected
        assert "cell state" in value.events and "cell beat" in value.events
        assert "services" not in value.events and "supervisor" not in value.events
        assert all(task.done() for task in value.tasks)
    finally:
        for task in value.tasks + value.producers:
            task.cancel()
        await asyncio.gather(*value.tasks, *value.producers, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_without_cell_keeps_its_existing_no_observation_boot(worker_boot, monkeypatch):
    value = worker_boot
    monkeypatch.setattr(settings, "cell", "")
    try:
        async with asyncio.timeout(1.0):
            await worker_main._serve(-1, -1)
        assert "cell state" not in value.events and "cell beat" not in value.events
        assert "supervisor" in value.events
        assert value.producers == []
    finally:
        for task in value.tasks + value.producers:
            task.cancel()
        await asyncio.gather(*value.tasks, *value.producers, return_exceptions=True)


class _FalseFailure(RuntimeError):
    def __bool__(self):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "error-close",
        "cancel-close",
        "false-close",
        "error-leave-cancel",
        "healthy-close",
        "healthy-both",
    ],
)
async def test_worker_cleanup_preserves_first_failure_and_attempts_both_closings(
    worker_boot, fault
):
    value = worker_boot
    healthy = fault.startswith("healthy")
    original = (
        asyncio.CancelledError("first cell write cancelled")
        if fault == "cancel-close"
        else _FalseFailure("first cell write false-valued failure")
        if fault == "false-close"
        else value.expected
    )
    leave_error = asyncio.CancelledError("leave cancelled")
    close_error = RuntimeError("pool close failed")
    value.fail[0] = not healthy
    value.first_error[0] = original
    if fault in {"error-leave-cancel", "healthy-both"}:
        value.cleanup_errors["leave"] = leave_error
    if fault != "error-leave-cancel":
        value.cleanup_errors["close"] = close_error
    expected = leave_error if fault == "healthy-both" else close_error if healthy else original
    try:
        async with asyncio.timeout(1.0):
            with pytest.raises(BaseException) as caught:
                await worker_main._serve(-1, -1)
        assert "retire" in value.events and "close" in value.events
        assert value.events.index("retire") < value.events.index("close")
        assert caught.value is expected
        assert all(task.done() for task in value.tasks + value.producers)
        assert value.gate.producers_stopped is healthy
        if not healthy:
            assert "services" not in value.events and "supervisor" not in value.events
    finally:
        for task in value.tasks + value.producers:
            task.cancel()
        await asyncio.gather(*value.tasks, *value.producers, return_exceptions=True)
