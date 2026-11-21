"""Record writers retain their explicit target across reconnects."""

import time
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.core import ch


@pytest.mark.asyncio
async def test_external_client_uses_only_explicit_connection_and_utc(monkeypatch):
    driver = SimpleNamespace(server_tz=None, close=AsyncMock())
    connect = AsyncMock(return_value=driver)
    monkeypatch.setattr(ch.clickhouse_connect, "get_async_client", connect)
    client = ch.ExternalCHClient(
        host="record.example",
        port=8443,
        secure=True,
        database="team_record",
        username="record_writer",
        password="secret",
    )
    await client.connect()
    assert connect.call_args.kwargs["host"] == "record.example"
    assert connect.call_args.kwargs["database"] == "team_record"
    assert connect.call_args.kwargs["username"] == "record_writer"
    assert driver.server_tz is UTC
    await client._reconnect(0)
    assert connect.call_count == 2
    assert connect.call_args.kwargs["database"] == "team_record"
    await client.disconnect()


@pytest.mark.asyncio
async def test_external_failed_connect_keeps_existing_client(monkeypatch):
    driver = SimpleNamespace(server_tz=None, close=AsyncMock())
    connect = AsyncMock(side_effect=[driver, OSError("connection refused")])
    monkeypatch.setattr(ch.clickhouse_connect, "get_async_client", connect)
    client = ch.ExternalCHClient(
        host="record.example",
        port=8443,
        secure=True,
        database="team_record",
        username="record_writer",
        password="secret",
    )
    await client.connect()
    with pytest.raises(OSError):
        await client._reconnect(0)
    assert client.client is driver and driver.close.await_count == 0
    await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment, external", [(1000.0, 1300.0), (1300.0, 1000.0)])
async def test_leased_insert_measures_its_own_target_clock(monkeypatch, deployment, external):
    from server.core import rmt_carry

    monkeypatch.setattr(rmt_carry, "_server_anchor", (deployment, time.monotonic()))
    monkeypatch.setattr(rmt_carry, "_skew_at", time.monotonic())
    client = ch.CHClient()
    client.query_one = AsyncMock(return_value={"ms": int(external * 1000)})
    client._column_type_names = AsyncMock(return_value=None)
    client._fenced_insert = AsyncMock()

    async def run(name, call, **kwargs):
        return await call()

    monkeypatch.setattr(client, "_with_retry", run)
    with rmt_carry.under_lease(time.monotonic() + 60):
        await client._insert_rows("feed_issues", [{"number": 1}], None)
    client.query_one.assert_awaited_once()
    deadline = client._fenced_insert.call_args.args[-1]
    assert external + 14 <= deadline <= external + 16
    assert rmt_carry._server_anchor[0] == deployment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_delay,response_delay,lease_left",
    [(28.0, 0.0, -5.0), (0.0, 28.0, -5.0), (14.0, 14.0, -5.0), (28.0, 0.0, 0.25)],
)
async def test_receiving_clock_never_extends_a_lease_deadline(
    monkeypatch, request_delay, response_delay, lease_left
):
    from server.core import rmt_carry
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock(t=100.0)
    monkeypatch.setattr(rmt_carry, "time", SimpleNamespace(monotonic=clock.monotonic))

    async def query_one(sql):
        clock.advance(request_delay)
        sampled_server = 1000.0 + clock.t - 100.0
        clock.advance(response_delay)
        return {"ms": int(sampled_server * 1000)}

    anchor = await rmt_carry.measure_send_clock(SimpleNamespace(query_one=query_one))
    clock.advance(3.0)
    actual_server = 1000.0 + clock.t - 100.0
    lease_end = clock.t + lease_left
    with rmt_carry.under_lease(lease_end):
        deadline = rmt_carry.clock_send_deadline(anchor)
    safe_deadline = actual_server + min(0.0, lease_left) + rmt_carry._TRANSIT_S
    estimated_server = anchor[0] + clock.t - anchor[1]
    assert estimated_server <= actual_server
    assert deadline <= safe_deadline
