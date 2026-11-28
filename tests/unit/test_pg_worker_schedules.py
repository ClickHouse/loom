from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from server.knowledge.domain.mcp_worker_refresh import RefreshRejected
from server.knowledge.domain.mcp_worker_schedule import ScheduleBinding, ScheduleRejected
from server.knowledge.store import pg_worker_schedules as store

_DUE = datetime(2026, 1, 1, tzinfo=UTC)
_BINDING = ScheduleBinding("org", "team", str(uuid4()), str(uuid4()), str(uuid4()), 2, 11, 4, 5)
_AUTH = store.ScheduleAuthority(
    _BINDING, "b" * 64, ("urn:document:selected",), "team", "alice", "alice", "agent", "-"
)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(store.pg, "enabled", lambda: True)


@pytest.fixture
def connection():
    conn = Mock(
        is_in_transaction=Mock(return_value=True),
        execute=AsyncMock(),
        fetchrow=AsyncMock(),
        fetchval=AsyncMock(),
    )
    conn.fetchrow.return_value = {
        "org": _BINDING.org,
        "namespace": _BINDING.namespace,
        "source_id": _BINDING.source_id,
        "connection_id": _BINDING.connection_id,
        "worker_id": _BINDING.worker_id,
        "connection_generation": _BINDING.connection_generation,
        "source_generation": _BINDING.source_generation,
        "selection_generation": _BINDING.selection_generation,
        "worker_generation": _BINDING.worker_generation,
        "user_id": "alice",
        "interval_seconds": 720,
        "next_due_at": _DUE,
        "paused": False,
    }
    conn.fetchval.side_effect = [_DUE + timedelta(seconds=730), 1]
    return conn


@pytest.mark.asyncio
async def test_due_copy_uses_selection_and_advances_after_enqueue(enabled, connection, monkeypatch):
    async def enqueue(conn, batch):
        assert conn is connection
        assert connection.fetchval.await_count == 1
        (entry,) = batch
        assert entry.due == _DUE
        assert entry.user_id == "alice"
        assert entry.task.resource_id == "urn:document:selected"
        assert entry.task.scope.approval_generation == 5
        assert entry.task.scope.profile_digest == "b" * 64
        return 1

    monkeypatch.setattr(store.pg_worker_refresh, "enqueue_batch_on", enqueue)
    assert await store.enqueue_due_on(connection, _AUTH) == 1
    assert connection.fetchval.await_args.args[-1] == _DUE + timedelta(seconds=1440)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [("user_id", "bob"), ("paused", True), ("source_generation", 12), ("worker_generation", 6)],
)
async def test_stale_or_paused_never_admits(enabled, connection, key, value):
    connection.fetchrow.return_value[key] = value
    assert await store.enqueue_due_on(connection, _AUTH) == 0
    connection.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_queue_admission_does_not_move_cursor(enabled, connection, monkeypatch):
    enqueue = AsyncMock(side_effect=RefreshRejected("queue limit"))
    monkeypatch.setattr(store.pg_worker_refresh, "enqueue_batch_on", enqueue)
    with pytest.raises(RefreshRejected, match="queue limit"):
        await store.enqueue_due_on(connection, _AUTH)
    enqueue.assert_awaited_once()
    connection.fetchval.assert_awaited_once_with("SELECT clock_timestamp()")


@pytest.mark.asyncio
async def test_busy_source_keeps_original_due_cursor(enabled, connection, monkeypatch):
    enqueue = AsyncMock(return_value=0)
    monkeypatch.setattr(store.pg_worker_refresh, "enqueue_batch_on", enqueue)
    assert await store.enqueue_due_on(connection, _AUTH) == 0
    enqueue.assert_awaited_once()
    connection.fetchval.assert_awaited_once_with("SELECT clock_timestamp()")


@pytest.mark.asyncio
async def test_due_admission_requires_transaction(enabled, connection):
    connection.is_in_transaction.return_value = False
    with pytest.raises(RefreshRejected, match="authority transaction"):
        await store.enqueue_due_on(connection, _AUTH)
    connection.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_growth_refuses_an_unsustainable_cadence(enabled, connection):
    larger = replace(_AUTH, selected=("urn:document:a", "urn:document:b"))
    with pytest.raises(ScheduleRejected, match="receipt capacity"):
        await store.enqueue_due_on(connection, larger)
    connection.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_claim_uses_wall_due_without_changing_offer_identity(
    enabled, connection, monkeypatch
):
    from server.knowledge.domain.mcp_worker_manual import ManualPosition, manual_demand, next_offer

    wall_now = _DUE - timedelta(minutes=2)
    selection = store._manual_selection(_AUTH)
    offer = next_offer(ManualPosition(uuid4(), 0), None, selection, _DUE)
    outer_slot = AsyncMock(
        return_value=(
            ("org", "team", uuid4()),
            {
                "manual_incarnation": offer.position.incarnation,
                "manual_sequence": offer.position.sequence,
                "manual_fingerprint": offer.fingerprint,
                "manual_issued_at": offer.issued_at,
                "manual_expires_at": offer.expires_at,
                "manual_accepted_items": None,
                "manual_clock_at": _DUE,
                "wall_now": wall_now,
            },
        )
    )
    stage = AsyncMock(return_value=store.ManualAdmission("enqueued", "fixture", 1))
    monkeypatch.setattr(store, "_manual_slot_on", outer_slot)
    monkeypatch.setattr(store, "_stage_manual_on", stage)
    assert (await store.submit_manual_on(connection, _AUTH, offer.position)).status == "enqueued"
    actual = stage.await_args.args[-1]
    expected = manual_demand(offer, offer.position, selection, now=_DUE)
    assert actual == replace(expected, due_at=wall_now)
