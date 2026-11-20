"""A coordination proof cannot cross a backend, credential or client replacement."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.core import ch


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.error: BaseException | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.closed = False

    async def query(self, sql: str, *, parameters: Any, settings: Any, **kwargs: Any) -> Any:
        self.calls.append((sql, dict(settings), kwargs))
        if self.release is not None:
            self.entered.set()
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(column_names=["value"], result_rows=[[1]], summary={})

    async def command(self, sql: str, *, parameters: Any, settings: Any, **kwargs: Any) -> None:
        self.calls.append((sql, dict(settings), kwargs))
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        self.closed = True

    def _force_close(self) -> None:
        self.closed = True


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> ch.CHClient:
    monkeypatch.setattr(ch.settings, "ch_reader_user", "read_only")
    connection = ch.CHClient()
    connection._client = _Driver()  # type: ignore[assignment]
    connection._connection_config = ch.connection_kwargs()
    channels: list[_Driver] = []
    connection.__dict__["proofs"] = channels

    async def create(**_kwargs: Any) -> _Driver:
        driver = _Driver()
        channels.append(driver)
        return driver

    monkeypatch.setattr(ch.clickhouse_connect, "get_async_client", create)
    return connection


@pytest.mark.asyncio
async def test_reads_and_writes_keep_one_checked_session_and_service_principal(client: Any) -> None:
    with ch.caller_shaped():
        await client.query_one("SELECT 1")
        async with client.coordination_session():
            await client.query_one("SELECT 2")
            await client.execute(
                "SELECT 3", settings={"session_id": "replacement", "session_check": 0}
            )
        await client.query_one("SELECT 4")
    calls = client.client.calls
    assert "Authorization" in calls[0][2]["transport_settings"]
    assert "Authorization" in calls[-1][2]["transport_settings"]
    proof = client.proofs[0].calls
    session_ids = {settings["session_id"] for _, settings, _ in proof}
    assert len(session_ids) == 1 and session_ids != {"replacement"}
    assert [settings["session_check"] for _, settings, _ in proof] == [0, 1, 1]
    assert all(kwargs == {} for _, _, kwargs in proof)
    assert all(
        settings["use_query_cache"] == settings["use_query_condition_cache"] == 0
        for _, settings, _ in proof
    )
    assert "session_id" not in calls[0][1] and "session_id" not in calls[-1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["generation", "driver"])
async def test_a_replaced_client_refuses_before_sending_another_query(
    client: Any, replacement: str
) -> None:
    original = client.client
    async with client.coordination_session():
        if replacement == "generation":
            client._generation += 1
        else:
            client._client = _Driver()
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 2")
    assert original.calls == [] and len(client.proofs[0].calls) == 1
    if replacement == "driver":
        assert client.client.calls == []


@pytest.mark.asyncio
async def test_a_network_failure_never_reconnects_or_reuses_a_negative_proof(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reconnect = AsyncMock()
    monkeypatch.setattr(client, "_reconnect", reconnect)
    error = ConnectionResetError("connection reset")
    async with client.coordination_session():
        driver = client._statement_client()
        driver.error = error
        with pytest.raises(ConnectionResetError) as caught:
            await client.query_one("SELECT 2")
        assert caught.value is error
        driver.error = None
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 3")
    assert len(driver.calls) == 2 and driver.closed
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ordinary_read_retains_its_existing_network_retry(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = client.client
    driver.error = ConnectionResetError("connection reset")

    async def reconnect(seen: int) -> None:
        driver.error = None
        client._generation += 1

    retry = AsyncMock(side_effect=reconnect)
    monkeypatch.setattr(client, "_reconnect", retry)
    assert await client.query_one("SELECT 2") == {"value": 1}
    retry.assert_awaited_once()
    assert len(driver.calls) == 2


@pytest.mark.asyncio
async def test_cancellation_invalidates_the_shared_proof_and_joins_the_query(client: Any) -> None:
    async with client.coordination_session():
        driver = client._statement_client()
        driver.release = asyncio.Event()
        pending = asyncio.create_task(client.query_one("SELECT 2"))
        await driver.entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pending.done()
        driver.release = None
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 3")
    assert len(driver.calls) == 2 and driver.closed


@pytest.mark.asyncio
async def test_a_child_cannot_reuse_the_session_after_its_owner_exits(client: Any) -> None:
    release = asyncio.Event()

    async def later() -> None:
        await release.wait()
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 2")

    async with client.coordination_session():
        pending = asyncio.create_task(later())
    release.set()
    await pending
    assert len(client.proofs[0].calls) == 1


@pytest.mark.asyncio
async def test_an_analyst_or_nested_client_cannot_change_the_protocol_identity(client: Any) -> None:
    with ch.as_analyst("analyst", "fake-password", {}):
        with pytest.raises(ch.CoordinationSessionLost):
            async with client.coordination_session():
                pytest.fail("an analyst entered privileged coordination")
    assert client.client.calls == []
    async with client.coordination_session():
        with pytest.raises(ch.CoordinationSessionLost):
            async with client.coordination_session():
                pytest.fail("a nested session replaced its owner")
        other = ch.CHClient()
        other._client = _Driver()
        with pytest.raises(ch.CoordinationSessionLost):
            await other.query_one("SELECT 2")
        assert other.client.calls == []


@pytest.mark.asyncio
async def test_concurrent_protocols_have_distinct_sessions_without_leaking_to_other_work(
    client: Any,
) -> None:
    release = asyncio.Event()
    arrived = 0

    async def proof() -> None:
        nonlocal arrived
        async with client.coordination_session():
            arrived += 1
            if arrived == 2:
                release.set()
            await release.wait()
            await client.query_one("SELECT 2")

    await asyncio.gather(proof(), proof())
    await client.query_one("SELECT 3")
    assert len(client.proofs) == 2
    assert sum(not driver.closed for driver in client.proofs) <= 1
    sessions = [
        settings["session_id"] for driver in client.proofs for _, settings, _ in driver.calls
    ]
    assert len(set(sessions)) == 2 and all(sessions.count(value) == 2 for value in set(sessions))
    assert len(client.client.calls) == 1 and "session_id" not in client.client.calls[0][1]
    await client.disconnect()
    assert all(driver.closed for driver in client.proofs)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_client", [False, True])
async def test_schema_refresh_stays_advisory_before_and_during_a_checked_session(
    client: Any, monkeypatch: pytest.MonkeyPatch, same_client: bool
) -> None:
    from server.core import ch_schema_guard
    from server.core.schema import status

    client._slots = asyncio.Semaphore(1)
    metadata = client if same_client else ch.CHClient()
    if not same_client:
        metadata._client = _Driver()  # type: ignore[assignment]
    monkeypatch.setattr(ch, "get_client", lambda: metadata)
    monkeypatch.setattr(status, "_cache", {"at": 0, "value": None})
    cache = {"at": None, "tables": {}, "refreshing": False}
    monkeypatch.setattr(ch_schema_guard, "_schema_guard", cache)
    async with client.coordination_session():
        cache["at"] = None
        status._cache["value"] = None
        await client.query_one("SELECT 2")
    assert len(metadata.client.calls) == 2
    assert all("session_id" not in extra for _, extra, _ in metadata.client.calls)
    assert [extra["session_check"] for _, extra, _ in client.proofs[0].calls] == [0, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["schema", "pool"])
async def test_preflight_refusal_invalidates_the_scope_without_sending(
    client: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async with client.coordination_session():
        refused = AsyncMock(side_effect=RuntimeError("bounded preflight refused"))
        target = "guard_pending_schema" if failure == "schema" else "_acquire_slot"
        monkeypatch.setattr(ch if failure == "schema" else client, target, refused)
        with pytest.raises(RuntimeError, match="bounded preflight refused"):
            await client.query_one("SELECT 2")
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 3")
    assert len(client.proofs[0].calls) == 1
    refused.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_pool_waiter_invalidates_the_scope_without_leaking_a_slot(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(_op_name: str) -> bool:
        entered.set()
        await release.wait()
        return False

    async with client.coordination_session():
        monkeypatch.setattr(client, "_acquire_slot", waiting)
        before = ch._POOL_IN_USE
        pending = asyncio.create_task(client.query_one("SELECT 2"))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pending.done() and ch._POOL_IN_USE == before
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 3")
    assert len(client.proofs[0].calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stream", "insert"])
async def test_unsupported_operations_refuse_before_any_send_and_poison_the_scope(
    client: Any, operation: str
) -> None:
    other = ch.CHClient()
    other._client = _Driver()  # type: ignore[assignment]
    async with client.coordination_session():
        with pytest.raises(ch.CoordinationSessionLost):
            if operation == "stream":
                async with other.raw_stream("SELECT 2"):
                    pytest.fail("streaming entered a coordination proof")
            else:
                await other.insert("loom._migration_lock", [{"name": "test"}])
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 3")
    assert len(client.proofs[0].calls) == 1 and other.client.calls == []


@pytest.mark.asyncio
async def test_sequential_proofs_reuse_transport_but_bootstrap_distinct_sessions(
    client: Any,
) -> None:
    for index in range(3):
        async with client.coordination_session():
            await client.query_one(f"SELECT {index + 2}")

    assert len(client.proofs) == 1
    driver = client.proofs[0]
    assert not driver.closed
    assert len(driver.calls) == 6
    sessions = [extra["session_id"] for _, extra, _ in driver.calls]
    assert len(set(sessions)) == 3
    assert all(sessions.count(value) == 2 for value in set(sessions))
    assert [extra["session_check"] for _, extra, _ in driver.calls] == [0, 1] * 3
    assert all(
        extra["use_query_cache"] == extra["use_query_condition_cache"] == 0
        for _, extra, _ in driver.calls
    )
    assert client.client.calls == []
    await client.disconnect()
    assert driver.closed


@pytest.mark.asyncio
async def test_a_caught_failure_discards_the_reusable_transport(client: Any) -> None:
    async with client.coordination_session():
        first_channel = client._statement_client()
        await client.query_one("SELECT 2")
    first = client.proofs[0]
    async with client.coordination_session():
        assert client._statement_client() is first_channel
        first.error = ConnectionResetError("connection reset")
        with pytest.raises(ConnectionResetError):
            await client.query_one("SELECT 3")
    assert first.closed
    async with client.coordination_session():
        assert client._statement_client() is not first_channel
        await client.query_one("SELECT 4")
    assert len(client.proofs) == 2
    await client.disconnect()
    assert all(driver.closed for driver in client.proofs)


@pytest.mark.asyncio
async def test_reusable_transport_is_not_shared_between_clients(client: Any) -> None:
    async with client.coordination_session():
        original_channel = client._statement_client()
        await client.query_one("SELECT 2")
    original = client.proofs[0]
    other = ch.CHClient()
    other._client = _Driver()
    other._connection_config = dict(client._connection_config)
    async with other.coordination_session():
        assert other._statement_client() is not original_channel
        await other.query_one("SELECT 3")
    assert len(client.proofs) == 2
    assert not original.closed
    await other.disconnect()
    assert not original.closed
    await client.disconnect()
    assert original.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["generation", "driver", "configuration"])
async def test_idle_transport_cannot_cross_a_connection_replacement(
    client: Any, replacement: str
) -> None:
    async with client.coordination_session():
        original_channel = client._statement_client()
        await client.query_one("SELECT 2")
    original = client.proofs[0]
    if replacement == "generation":
        client._generation += 1
    elif replacement == "driver":
        client._client = _Driver()
    else:
        client._connection_config = dict(client._connection_config, username="different")
    async with client.coordination_session():
        assert client._statement_client() is not original_channel
        await client.query_one("SELECT 3")
    assert len(client.proofs) == 2 and original.closed
    await client.disconnect()
    assert all(driver.closed for driver in client.proofs)


@pytest.mark.asyncio
async def test_owner_exit_discards_a_channel_with_an_inflight_child(client: Any) -> None:
    pending = None
    try:
        async with client.coordination_session():
            original_channel = client._statement_client()
            original = client.proofs[0]
            original.release = asyncio.Event()
            pending = asyncio.create_task(client.query_one("SELECT 2"))
            await original.entered.wait()
        assert original.closed
        async with client.coordination_session():
            assert client._statement_client() is not original_channel
            await client.query_one("SELECT 3")
    finally:
        if pending is not None:
            original.release.set()
            outcomes = await asyncio.gather(pending, return_exceptions=True)
        await client.disconnect()
    assert isinstance(outcomes[0], ch.CoordinationSessionLost)
    assert all(driver.closed for driver in client.proofs)


@pytest.mark.asyncio
async def test_idle_transport_reuse_never_revives_an_old_child_session(client: Any) -> None:
    release = asyncio.Event()

    async def later() -> None:
        await release.wait()
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 2")

    pending = None
    try:
        async with client.coordination_session():
            original_channel = client._statement_client()
            pending = asyncio.create_task(later())
        async with client.coordination_session():
            assert client._statement_client() is original_channel
            release.set()
            await pending
            await client.query_one("SELECT 3")
        assert len(client.proofs) == 1 and len(client.proofs[0].calls) == 3
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await client.disconnect()
