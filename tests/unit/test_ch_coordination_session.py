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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> ch.CHClient:
    monkeypatch.setattr(ch.settings, "ch_reader_user", "read_only")
    connection = ch.CHClient()
    connection._client = _Driver()  # type: ignore[assignment]
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
    proof = calls[1:-1]
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
    assert len(original.calls) == 1
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
        client.client.error = error
        with pytest.raises(ConnectionResetError) as caught:
            await client.query_one("SELECT 2")
        assert caught.value is error
        client.client.error = None
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 3")
    assert len(client.client.calls) == 2
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
        client.client.release = asyncio.Event()
        pending = asyncio.create_task(client.query_one("SELECT 2"))
        await client.client.entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert pending.done()
        client.client.release = None
        with pytest.raises(ch.CoordinationSessionLost):
            await client.query_one("SELECT 3")
    assert len(client.client.calls) == 2


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
    assert len(client.client.calls) == 1


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
    calls = client.client.calls
    sessions = [settings["session_id"] for _, settings, _ in calls[:-1]]
    assert len(set(sessions)) == 2 and all(sessions.count(value) == 2 for value in set(sessions))
    assert "session_id" not in calls[-1][1]


@pytest.mark.asyncio
async def test_schema_refresh_stays_advisory_before_and_during_a_checked_session(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ch_schema_guard
    from server.core.schema import status

    metadata = ch.CHClient()
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
    assert [extra["session_check"] for _, extra, _ in client.client.calls] == [0, 1]


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
    assert len(client.client.calls) == 1
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
    assert len(client.client.calls) == 1


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
    assert len(client.client.calls) == 1 and other.client.calls == []
