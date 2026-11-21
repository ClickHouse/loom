"""A named coordination session belongs to one transport, not the shared pool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

from server.core import ch


class _RoutedDriver:
    def __init__(self, *, pooled: bool) -> None:
        self.pooled = pooled
        self.calls: list[dict[str, Any]] = []
        self.sessions: dict[int, set[str]] = {0: set(), 1: set()}
        self.closed = False
        self.fail_at: int | None = None
        self.close_calls = 0
        self.force_calls = 0

    def _send(self, settings: dict[str, Any]) -> None:
        connection = len(self.calls) % 2 if self.pooled else 0
        self.calls.append(dict(settings))
        if len(self.calls) == self.fail_at:
            raise DatabaseError("Code: 372. SESSION_NOT_FOUND")
        if "session_id" not in settings:
            return
        identifier = settings["session_id"]
        if settings["session_check"]:
            if identifier not in self.sessions[connection]:
                raise DatabaseError("Code: 372. SESSION_NOT_FOUND")
        else:
            self.sessions[connection].add(identifier)

    async def query(self, _sql: str, *, settings: Any, **_kwargs: Any) -> Any:
        self._send(settings)
        return SimpleNamespace(column_names=["value"], result_rows=[[1]], summary={})

    async def command(self, _sql: str, *, settings: Any, **_kwargs: Any) -> None:
        self._send(settings)

    async def raw_query(self, _sql: str, *, settings: Any, **_kwargs: Any) -> bytes:
        self._send(settings)
        return b"1"

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def _force_close(self) -> None:
        self.force_calls += 1
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["query", "command", "raw"])
async def test_proof_uses_an_owned_transport_instead_of_rotating_the_shared_pool(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    shared = _RoutedDriver(pooled=True)
    channels: list[_RoutedDriver] = []
    options: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> _RoutedDriver:
        options.append(kwargs)
        channel = _RoutedDriver(pooled=False)
        channels.append(channel)
        return channel

    monkeypatch.setattr(ch, "guard_pending_schema", AsyncMock())
    monkeypatch.setattr(ch.clickhouse_connect, "get_async_client", create)
    client: Any = ch.CHClient()
    client._client = shared
    client._connection_config = ch.connection_kwargs()
    async with client.coordination_session():
        if operation == "query":
            assert await client.query_one("SELECT 1") == {"value": 1}
        elif operation == "command":
            await client.execute("SELECT 1")
        else:
            assert await client.raw_bytes("SELECT 1") == b"1"
    assert client.client is shared and shared.calls == [] and not shared.closed
    assert len(channels) == 1 and not channels[0].closed
    assert channels[0].close_calls == 0 and channels[0].force_calls == 0
    assert [call["session_check"] for call in channels[0].calls] == [0, 1]
    assert len({call["session_id"] for call in channels[0].calls}) == 1
    assert {key: options[0][key] for key in client._connection_config} == client._connection_config
    assert options[0]["connector_limit"] == options[0]["connector_limit_per_host"] == 1
    assert options[0]["query_retries"] == 0
    assert options[0]["settings"]["use_query_cache"] == 0
    assert options[0]["settings"]["use_query_condition_cache"] == 0
    await client.disconnect()
    assert channels[0].closed and channels[0].close_calls == 1
    assert channels[0].force_calls == 0


def _client_with_channel(
    monkeypatch: pytest.MonkeyPatch, channel: _RoutedDriver
) -> tuple[Any, AsyncMock]:
    client: Any = ch.CHClient()
    client._client = _RoutedDriver(pooled=True)
    client._connection_config = ch.connection_kwargs()
    factory = AsyncMock(return_value=channel)
    monkeypatch.setattr(ch, "guard_pending_schema", AsyncMock())
    monkeypatch.setattr(ch.clickhouse_connect, "get_async_client", factory)
    return client, factory


@pytest.mark.asyncio
async def test_a_created_channel_is_owned_before_the_generation_postcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _RoutedDriver(pooled=False)
    client, factory = _client_with_channel(monkeypatch, channel)
    capacity = client._stream_slots._value

    async def replaced(**_kwargs: Any) -> _RoutedDriver:
        client._generation += 1
        return channel

    factory.side_effect = replaced
    with pytest.raises(ch.CoordinationSessionLost):
        async with client.coordination_session():
            pytest.fail("a changed owner admitted the proof")
    assert channel.closed and channel.force_calls == 1 and channel.close_calls == 0
    assert channel.calls == [] and client._stream_slots._value == capacity
    assert not client.client.closed and ch._coordination_session.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [1, 2])
async def test_a_missing_checked_session_closes_only_its_channel_without_rebootstrap(
    monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    channel = _RoutedDriver(pooled=False)
    channel.fail_at = fail_at
    client, factory = _client_with_channel(monkeypatch, channel)
    reconnect = AsyncMock()
    monkeypatch.setattr(client, "_reconnect", reconnect)
    capacity = client._stream_slots._value
    with pytest.raises(DatabaseError, match="SESSION_NOT_FOUND"):
        async with client.coordination_session():
            await client.query_one("SELECT 1")
    assert len(channel.calls) == fail_at and channel.force_calls == 1
    assert channel.closed and not client.client.closed and client.client.calls == []
    assert client._stream_slots._value == capacity and ch._coordination_session.get() is None
    factory.assert_awaited_once()
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_disconnect_force_closes_the_idle_channel_and_keeps_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _RoutedDriver(pooled=False)
    monkeypatch.setattr(channel, "close", AsyncMock(side_effect=asyncio.CancelledError()))
    client, _ = _client_with_channel(monkeypatch, channel)
    capacity = client._stream_slots._value
    async with client.coordination_session():
        pass
    assert not channel.closed
    with pytest.raises(asyncio.CancelledError):
        await client.disconnect()
    assert channel.closed and channel.force_calls == 1 and not client.connected
    assert client._stream_slots._value == capacity and ch._coordination_session.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["transport", "pool"])
async def test_cancelled_admission_does_not_allocate_a_channel_or_leak_capacity(
    monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    channel = _RoutedDriver(pooled=False)
    client, factory = _client_with_channel(monkeypatch, channel)
    entered = asyncio.Event()
    in_use = ch._POOL_IN_USE
    if gate == "transport":
        client._stream_slots = _ObservedSlots(0)
        entered = client._stream_slots.waiting
    else:
        client._slots = asyncio.Semaphore(0)
        acquire = client._acquire_slot

        async def wait_pool(what: str) -> bool:
            entered.set()
            return bool(await acquire(what))

        monkeypatch.setattr(client, "_acquire_slot", wait_pool)
    capacity = client._stream_slots._value

    async def proof() -> None:
        async with client.coordination_session():
            pytest.fail("occupied capacity admitted a proof")

    pending = asyncio.create_task(proof())
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert pending.done() and ch._POOL_IN_USE == in_use
    assert client._stream_slots._value == capacity and ch._coordination_session.get() is None
    factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_admitted_proof_waits_for_the_existing_transport_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _RoutedDriver(pooled=False)
    client, factory = _client_with_channel(monkeypatch, channel)
    client._stream_slots = _ObservedSlots(0)
    entered = client._stream_slots.waiting

    async def proof() -> None:
        async with client.coordination_session():
            assert client._stream_slots._value == 0

    pending = asyncio.create_task(proof())
    await entered.wait()
    factory.assert_not_awaited()
    client._stream_slots.release()
    await pending
    assert pending.done() and client._stream_slots._value == 1
    assert not channel.closed and not client.client.closed
    factory.assert_awaited_once()
    await client.disconnect()
    assert channel.closed


@pytest.mark.asyncio
async def test_ordinary_work_can_use_the_shared_driver_while_a_proof_owns_its_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _RoutedDriver(pooled=False)
    client, _ = _client_with_channel(monkeypatch, channel)
    release = asyncio.Event()

    async def ordinary() -> Any:
        await release.wait()
        return await client.query_one("SELECT 1")

    pending = asyncio.create_task(ordinary())
    async with client.coordination_session():
        release.set()
        assert await pending == {"value": 1}
        assert len(client.client.calls) == 1 and "session_id" not in client.client.calls[0]
        assert len(channel.calls) == 1
        assert await client.query_one("SELECT 1") == {"value": 1}
    assert not channel.closed and not client.client.closed
    await client.disconnect()
    assert channel.closed


@pytest.mark.asyncio
async def test_ordinary_connect_keeps_its_pool_settings_and_startup_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _RoutedDriver(pooled=True)
    factory = AsyncMock(return_value=driver)
    cache_probe, vector_probe = AsyncMock(), AsyncMock()
    monkeypatch.setattr(ch.clickhouse_connect, "get_async_client", factory)
    monkeypatch.setattr(ch, "_turn_off_unsafe_condition_cache", cache_probe)
    monkeypatch.setattr(ch, "_probe_vector_state", vector_probe)
    monkeypatch.setattr(ch.settings, "ch_pool_maxsize", 3)
    client = ch.CHClient()
    await client.connect()
    options = factory.await_args.kwargs
    assert {key: options[key] for key in client._connection_config} == client._connection_config
    assert options["connector_limit"] == options["connector_limit_per_host"] == 3 + ch.STREAM_SLOTS
    assert "query_retries" not in options
    assert options["settings"] == {
        "async_insert": 0,
        "wait_for_async_insert": 1,
        "insert_deduplicate": 0,
        "enable_filesystem_cache": 1,
        "session_timezone": "UTC",
    }
    assert client.client.server_tz is ch.UTC
    cache_probe.assert_awaited_once_with(client.client)
    vector_probe.assert_awaited_once_with(client)
    await client.disconnect()
    assert driver.closed


class _ObservedSlots(asyncio.Semaphore):
    def __init__(self, value: int) -> None:
        super().__init__(value)
        self.waiting = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


@pytest.mark.asyncio
async def test_transport_queue_uses_the_original_proof_deadline_without_a_fifth_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.clock_fake import ManualClock

    client, factory = _client_with_channel(monkeypatch, _RoutedDriver(pooled=False))
    channels: list[_RoutedDriver] = []

    async def create(**_kwargs: Any) -> _RoutedDriver:
        channel = _RoutedDriver(pooled=False)
        channels.append(channel)
        return channel

    factory.side_effect = create
    slots = _ObservedSlots(ch.STREAM_SLOTS)
    client._stream_slots = slots
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock)
    entered = [asyncio.Event() for _ in range(ch.STREAM_SLOTS + 1)]
    release = [asyncio.Event() for _ in entered]

    async def proof(index: int) -> None:
        async with asyncio.timeout(5), client.coordination_session():
            entered[index].set()
            await release[index].wait()

    tasks = [asyncio.create_task(proof(i)) for i in range(ch.STREAM_SLOTS)]
    try:
        await asyncio.gather(*(event.wait() for event in entered[:-1]))
        tasks.append(asyncio.create_task(proof(ch.STREAM_SLOTS)))
        await slots.waiting.wait()
        clock.advance(0.6)
        # Deliver any due short-admission timeout and its cancellation callbacks.
        for _ in range(8):
            await asyncio.sleep(0)
        assert not tasks[-1].done(), "transport queue spent the statement admission budget"
        assert len(channels) == ch.STREAM_SLOTS and slots._value == 0
        release[0].set()
        await entered[-1].wait()
        assert not channels[0].closed and len(channels) == ch.STREAM_SLOTS
        assert len({call["session_id"] for call in channels[0].calls}) == 2
        assert sum(not channel.closed for channel in channels) == ch.STREAM_SLOTS
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(not channel.closed for channel in channels) == 1
    assert slots._value == ch.STREAM_SLOTS
    assert not client.client.closed
    await client.disconnect()
    assert all(channel.closed for channel in channels)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["transport", "statement"])
async def test_waiting_for_capacity_retains_the_proof_and_statement_deadlines(
    monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    from tests.unit.clock_fake import ManualClock

    client, factory = _client_with_channel(monkeypatch, _RoutedDriver(pooled=False))
    slots = _ObservedSlots(0)
    if gate == "transport":
        client._stream_slots = slots
    else:
        client._slots = slots
    transport_capacity = client._stream_slots._value
    in_use = ch._POOL_IN_USE
    loop = asyncio.get_running_loop()
    clock = ManualClock(t=loop.time())
    monkeypatch.setattr(loop, "time", clock)

    async def proof() -> None:
        async with asyncio.timeout(5), client.coordination_session():
            pytest.fail("occupied capacity admitted the proof")

    pending = asyncio.create_task(proof())
    try:
        await slots.waiting.wait()
        clock.advance(5.1 if gate == "transport" else 0.6)
        expected = TimeoutError if gate == "transport" else ch.OverloadedError
        with pytest.raises(expected):
            await pending
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    factory.assert_not_awaited()
    assert slots._value == 0 and client._stream_slots._value == transport_capacity
    assert ch._POOL_IN_USE == in_use and ch._coordination_session.get() is None
