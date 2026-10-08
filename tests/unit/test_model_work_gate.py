"""Draining never confuses a timeout with cancellation or successful quiescence."""

import asyncio

import pytest

from server.core.model_work_gate import ModelChangeInProgress, ModelWorkGate


@pytest.mark.asyncio
async def test_timeout_retains_running_work_and_keeps_admission_closed():
    gate = ModelWorkGate()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def work():
        async with gate.admit():
            entered.set()
            await finish.wait()

    task = asyncio.create_task(work())
    await entered.wait()
    gate.close("transition")
    assert not await gate.wait_drained("transition", timeout_s=0)
    assert not task.done()
    assert gate.active == 1
    with pytest.raises(ModelChangeInProgress):
        async with gate.admit():
            pytest.fail("new request was admitted")
    with pytest.raises(RuntimeError, match="not drained"):
        gate.resume("transition")
    finish.set()
    await task
    assert await gate.wait_drained("transition", timeout_s=0)
    gate.resume("transition")
    assert not gate.closed


@pytest.mark.asyncio
async def test_waiting_worker_resumes_only_after_the_owner_reopens():
    gate = ModelWorkGate()
    gate.close("transition")
    entered = asyncio.Event()

    async def work():
        async with gate.admit(wait=True):
            entered.set()

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    assert not entered.is_set()
    assert gate.active == 0
    assert await gate.wait_drained("transition", timeout_s=0)
    gate.resume("transition")
    await task
    assert entered.is_set()
    assert gate.active == 0


@pytest.mark.asyncio
async def test_cancelling_a_drain_wait_does_not_cancel_the_admitted_task():
    gate = ModelWorkGate()
    async with gate.admit():
        gate.close("transition")
        drain = asyncio.create_task(gate.wait_drained("transition", timeout_s=10))
        await asyncio.sleep(0)
        drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain
        assert gate.active == 1
        assert gate.closed
    assert await gate.wait_drained("transition", timeout_s=0)


@pytest.mark.asyncio
async def test_exception_and_cancellation_release_admission():
    gate = ModelWorkGate()
    for error in (ValueError("test"), asyncio.CancelledError()):
        with pytest.raises(type(error)):
            async with gate.admit():
                raise error
        assert gate.active == 0


@pytest.mark.asyncio
async def test_stale_transition_cannot_drain_or_reopen_a_new_transition():
    gate = ModelWorkGate()
    gate.close("first")
    gate.close("first")
    with pytest.raises(ValueError):
        gate.close("second")
    gate.resume("first")
    gate.close("second")
    with pytest.raises(ValueError):
        await gate.wait_drained("first", timeout_s=0)
    with pytest.raises(ValueError):
        gate.resume("first")
    assert gate.closed
