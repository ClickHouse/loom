"""Native completion waits for real retirement checks and observes tracked outcomes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.cli import _ch
from server.core import retirement, rmt_carry
from server.insights.app import provider_barrier as barrier
from server.insights.app import provider_command as command
from tests.unit.test_cli_native_first_error import FIRST
from tests.unit.test_cli_native_first_error import native as native


@pytest.mark.parametrize("first_class", [None, *FIRST])
def test_healthy_heartbeat_finishes_after_native_owner_requests_stop(
    native, monkeypatch, first_class
):
    requested = []
    attempts = []
    heartbeat = SimpleNamespace(running=True)

    def stop():
        requested.append(True)

    async def settle(client):
        assert client is native.client
        assert requested and heartbeat.running
        attempts.append(True)
        heartbeat.running = False

    heartbeat.signal_stop = stop
    monkeypatch.setattr(barrier, "_HEARTBEAT", heartbeat)
    monkeypatch.setattr(rmt_carry, "settle", settle)
    first = first_class("work") if first_class else None

    async def work(_):
        if first is not None:
            raise first
        return 7

    if first is None:
        assert _ch.run_connected(work, orgs=("o",)) == 7
    else:
        with pytest.raises(first_class) as caught:
            _ch.run_connected(work, orgs=("o",))
        assert caught.value is first
    assert attempts == [True]
    assert not heartbeat.running
    assert len(native.terminal_ends) == native.events.count("pg") == 2
    assert native.terminal_ends[0] == native.terminal_ends[1]
    assert not native.reversible_closes
    assert native.events[-3:] == ["pg", "certificate", "ch"]


@pytest.mark.parametrize("first_class", [None, *FIRST])
def test_permanent_heartbeat_respects_same_absolute_native_allowance(
    native, monkeypatch, first_class
):
    monkeypatch.setattr(command.settings, "native_command_drain_timeout_s", 0.02)
    monkeypatch.setattr(
        barrier, "_HEARTBEAT", SimpleNamespace(signal_stop=lambda: None, running=True)
    )
    monkeypatch.setattr(rmt_carry, "settle", AsyncMock())
    first = first_class("work") if first_class else None
    elapsed = []

    async def work(_):
        loop = asyncio.get_running_loop()
        elapsed.append((loop, loop.time()))
        if first is not None:
            raise first
        return 7

    with pytest.raises(first_class or TimeoutError) as caught:
        _ch.run_connected(work, orgs=("o",))
    if first is not None:
        assert caught.value is first
    loop, start = elapsed[0]
    assert loop.time() - start < 0.5
    assert "certificate" not in native.events
    assert native.events[-2:] == ["pg", "ch"]


@pytest.mark.parametrize("first_class", [None, ValueError, asyncio.CancelledError])
def test_repeated_owner_cancellation_keeps_first_and_allows_finite_heartbeat_completion(
    native, monkeypatch, first_class
):
    first = first_class("work") if first_class else None
    heartbeat = SimpleNamespace(running=True, signal_stop=lambda: None)
    attempts = []
    cancellations = []

    async def settle(client):
        assert client is native.client
        owner = asyncio.current_task()
        attempts.append(True)
        if len(attempts) <= 2:
            owner.cancel("first signal" if len(attempts) == 1 else "second signal")
        else:
            heartbeat.running = False
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    monkeypatch.setattr(barrier, "_HEARTBEAT", heartbeat)
    monkeypatch.setattr(rmt_carry, "settle", settle)

    async def work(_):
        if first is not None:
            raise first
        return 7

    with pytest.raises(first_class or asyncio.CancelledError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is (first if first is not None else cancellations[0])
    assert len(attempts) == 3 and len(cancellations) == 2
    assert len(native.terminal_ends) == native.events.count("pg") == len(attempts) + 1
    assert native.terminal_ends == [native.terminal_ends[0]] * (len(attempts) + 1)
    assert not native.reversible_closes
    assert native.events[-3:] == ["pg", "certificate", "ch"]


@pytest.mark.parametrize("first_class", [None, *FIRST])
def test_real_track_completed_failure_is_observed_before_native_cleanup(native, first_class):
    producer_error = OSError("producer")
    first = first_class("work") if first_class else None
    tasks = []

    async def work(_):
        async def producer():
            raise producer_error

        task = native.gate.track(asyncio.create_task(producer()))
        tasks.append(task)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not native.gate.producers
        if first is not None:
            raise first
        return 7

    with pytest.raises(first_class or OSError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is (first if first is not None else producer_error)
    assert native.events[-3:] == ["pg", "certificate", "ch"]
    assert all(task.done() and not task._log_traceback for task in tasks)


@pytest.mark.asyncio
async def test_native_track_retains_only_first_failure_and_no_completed_task_handles():
    gate = retirement.RetirementGate()
    gate.install("command")
    first = OSError("first producer")
    tasks = []

    async def producer(error=None):
        if error is not None:
            raise error

    for error in [None] * 100 + [first, ValueError("later")] + [None] * 100:
        task = gate.track(asyncio.create_task(producer(error)))
        tasks.append(task)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert gate.producer_failure is first
    assert gate.producers == set()
    assert all(not task._log_traceback for task in tasks)
    assert retirement.RetirementGate().producer_failure is None


@pytest.mark.asyncio
async def test_intentionally_cancelled_tracked_native_producer_is_not_failed():
    gate = retirement.RetirementGate()
    gate.install("command")
    task = gate.track(asyncio.create_task(asyncio.Event().wait()))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled() and gate.producers == set()
    assert gate.producer_failure is None


@pytest.mark.parametrize("role", ["server", "worker"])
@pytest.mark.asyncio
async def test_other_role_tracking_keeps_existing_outcome_ownership(role):
    gate = retirement.RetirementGate()
    gate.install(role)
    first = OSError("role-owned producer")

    async def producer():
        raise first

    task = gate.track(asyncio.create_task(producer()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gate.producers == set() and gate.producer_failure is None
    assert task._log_traceback
    assert task.exception() is first


def test_actual_calibration_consumer_failure_survives_finished_track_callback(native, monkeypatch):
    from server.memory.app import embedder_calibration

    first = OSError("calibration producer")
    tasks = []

    async def sleep(_):
        raise first

    monkeypatch.setattr(embedder_calibration, "_refresh_task", None)
    monkeypatch.setattr(
        embedder_calibration,
        "asyncio",
        SimpleNamespace(sleep=sleep, get_running_loop=asyncio.get_running_loop),
    )

    async def configuration():
        tasks.append(embedder_calibration.start_refresh_loop())

    monkeypatch.setattr(command, "load_active_configuration", configuration)

    async def work(_):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not native.gate.producers
        return 7

    with pytest.raises(OSError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first
    assert tasks[0].done() and not tasks[0]._log_traceback
    assert native.events[-3:] == ["pg", "certificate", "ch"]


@pytest.mark.parametrize("phase", ["flush", "certificate"])
def test_dependency_cancellation_is_not_retried_as_owner_cancellation(native, monkeypatch, phase):
    first = asyncio.CancelledError("cleanup dependency cancelled")
    required = AsyncMock(
        side_effect=[first, RuntimeError("cleanup retried without owner cancellation")]
    )
    target = "flush_registered_buffers" if phase == "flush" else "_retire_until"
    monkeypatch.setattr(command, target, required)

    async def work(_):
        return 7

    with pytest.raises(asyncio.CancelledError) as caught:
        _ch.run_connected(work, orgs=("o",))
    assert caught.value is first
    assert required.await_count == 1
    assert "pg" not in native.events and "certificate" not in native.events
    assert not native.terminal_ends and not native.reversible_closes
    assert native.events[-2:] == ["stop" if phase == "flush" else "flush", "ch"]
    assert not native.client.connected
    assert native.gate.closed and native.gate.active == 0
