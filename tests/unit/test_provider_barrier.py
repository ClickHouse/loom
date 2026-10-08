"""A positive census, not a timeout, permits rebuilding shared vectors."""

from contextlib import contextmanager
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from server.core.model_work_gate import ModelWorkGate
from server.insights.app import provider_barrier as barrier


@pytest.fixture
def setup(monkeypatch):
    identity, transition = uuid4(), uuid4()
    gate = ModelWorkGate()
    monkeypatch.setattr(barrier, "get_model_work_gate", lambda: gate)
    monkeypatch.setattr(barrier, "process_identity", lambda: identity)
    monkeypatch.setattr(barrier, "drain_task_sets", AsyncMock(return_value=True))
    monkeypatch.setattr(barrier, "flush_registered_buffers", AsyncMock(return_value=True))
    record = AsyncMock()
    monkeypatch.setattr(barrier, "record_participant", record)
    monkeypatch.setattr(barrier, "storage_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(
        barrier,
        "participants",
        AsyncMock(
            return_value=[
                {
                    "participant_id": identity,
                    "transition_id": transition,
                    "state": "drained",
                    "version": 1,
                }
            ]
        ),
    )
    return identity, transition, gate, record


@pytest.mark.asyncio
async def test_active_request_prevents_ack(setup):
    _, transition, gate, record = setup
    async with gate.admit():
        assert not await barrier.acknowledge_drain(transition)
    assert gate.closed
    record.assert_not_awaited()
    assert await barrier.acknowledge_drain(transition)
    assert record.call_args.args[1:] == (transition, "drained")


@pytest.mark.asyncio
async def test_buffer_flush_must_not_hide_a_new_deferred_writer(setup, monkeypatch):
    _, transition, gate, record = setup
    monkeypatch.setattr(barrier, "drain_task_sets", AsyncMock(side_effect=[True, False]))
    assert not await barrier.acknowledge_drain(transition)
    assert gate.closed
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_registered_process_must_ack_same_transition(setup, monkeypatch):
    identity, transition, _, record = setup
    other = uuid4()
    rows = [
        {"participant_id": identity, "transition_id": transition, "state": "drained", "version": 1},
        {"participant_id": other, "transition_id": UUID(int=0), "state": "drained", "version": 1},
    ]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))

    @contextmanager
    def alive(_):
        yield False

    monkeypatch.setattr(barrier, "stopped_process", alive)
    assert not await barrier.distributed_drain(transition)
    record.assert_not_awaited()
    rows[1]["transition_id"] = transition
    assert await barrier.distributed_drain(transition)


@pytest.mark.asyncio
async def test_kernel_proof_plus_storage_idle_retires_a_crashed_process(setup, monkeypatch):
    identity, transition, _, record = setup
    other = uuid4()
    monkeypatch.setattr(
        barrier,
        "participants",
        AsyncMock(
            return_value=[
                {
                    "participant_id": identity,
                    "transition_id": transition,
                    "state": "drained",
                    "version": 1,
                },
                {
                    "participant_id": other,
                    "transition_id": UUID(int=0),
                    "state": "running",
                    "version": 100,
                },
            ]
        ),
    )

    @contextmanager
    def stopped(_):
        yield True

    monkeypatch.setattr(barrier, "stopped_process", stopped)
    monkeypatch.setattr(barrier, "storage_idle", AsyncMock(return_value=False))
    assert not await barrier.distributed_drain(transition)
    record.assert_not_awaited()
    monkeypatch.setattr(barrier, "storage_idle", AsyncMock(return_value=True))
    assert await barrier.distributed_drain(transition)
    record.assert_awaited_once_with(other, transition, "retired", after=100)


@pytest.mark.asyncio
async def test_empty_census_or_missing_coordinator_is_not_a_barrier(setup, monkeypatch):
    _, transition, _, _ = setup
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=[]))
    assert not await barrier.distributed_drain(transition)


@pytest.mark.asyncio
async def test_retirement_refuses_pending_writers(setup, monkeypatch):
    _, _, _, record = setup
    monkeypatch.setattr(barrier, "drain_task_sets", AsyncMock(return_value=False))
    assert not await barrier.retire_process()
    record.assert_not_awaited()
