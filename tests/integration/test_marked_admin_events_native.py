"""Only verified operator reads retain private event fields and measurement samples."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.insights.store import admin
from server.observability.events import AgentEvent
from tests.integration._event_fixtures import insert_event_rows

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest_asyncio.fixture
async def reader() -> AsyncIterator[Any]:
    client = CHClient()
    database = "admin_marked_" + uuid4().hex
    database_uuid = str(uuid4())
    attempted = False
    try:
        await client.connect()
        attempted = True
        await client.execute(f"CREATE DATABASE {database} UUID '{database_uuid}' ENGINE = Atomic")
        await client.execute(f"CREATE TABLE {database}.agent_events AS loom.agent_events")
        await client.execute(
            f"CREATE TABLE {database}.agent_events_private AS loom.agent_events_private"
        )

        async def query_all(sql: str, params: Any = None) -> Any:
            return await client.query_all(
                sql.replace("loom.agent_events", f"{database}.agent_events"), params=params
            )

        ordinary = AgentEvent(
            ts=datetime.now(UTC),
            org="example",
            namespace="notes",
            agent="ordinary",
            user_id="reader",
            session_id="session",
            operation="memory.ask",
            request_id=uuid4().hex,
            latency_ms=7,
            success=True,
            consumer="client",
            kind="request",
            upstream_cost_usd=0.25,
            args={"consumer_source": "claimed"},
        ).to_row()
        marked = {
            **ordinary,
            "request_id": uuid4().hex,
            "agent": "private",
            "mirror_marked": 1,
            "success": False,
            "error_code": "PRIVATE_FAILURE",
            "latency_ms": 999,
            "upstream_cost_usd": 13,
        }
        await insert_event_rows(
            client,
            f"{database}.agent_events",
            [ordinary, marked, {**marked, "namespace": "acl-" + "a" * 24}],
            async_insert_settings(),
            required_columns={"mirror_marked"},
        )
        yield SimpleNamespace(query_all=query_all)
    finally:
        try:
            if attempted:
                owned = await client.query_one(
                    "SELECT toString(uuid) AS uuid FROM system.databases WHERE name = {name:String}",
                    params={"name": database},
                )
                if owned:
                    assert owned["uuid"] == database_uuid, "Private database identity changed"
                    await client.execute(f"DROP DATABASE {database} SYNC")
        finally:
            await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True])
async def test_event_tail_keeps_ordinary_rows_and_masks_private_fields(
    reader: Any, operator: bool
) -> None:
    rows = await admin.events_tail(
        3600, 10, filters={"org": "example"}, operator=operator, ch=reader
    )
    assert len(rows) == 2
    private = next(r for r in rows if r["agent"] == "private")
    assert private["latency_ms"] == (999 if operator else 0)
    assert private["error_code"] == ("PRIVATE_FAILURE" if operator else "")
    assert next(r for r in rows if r["agent"] == "ordinary")["latency_ms"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True])
async def test_consumer_error_latency_and_cost_samples_are_ordinary_below_operator(
    reader: Any, operator: bool
) -> None:
    rows = await admin.consumers(3600, 10, org="example", operator=operator, ch=reader)
    assert len(rows) == 1
    row = rows[0]
    assert row["calls"] == (2 if operator else 1)
    assert row["errors"] == (1 if operator else 0)
    assert float(row["spend_usd"]) == (13.25 if operator else 0.25)
    assert float(row["error_rate"]) == (0.5 if operator else 0)
    if not operator:
        assert row["p50_ms"] == row["p95_ms"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True])
async def test_tagging_uses_ordinary_rows_below_operator(reader: Any, operator: bool) -> None:
    rows = await admin.consumer_tagging(3600, 10, org="example", operator=operator, ch=reader)
    assert {r["agent"] for r in rows} == ({"ordinary", "private"} if operator else {"ordinary"})
    assert all(r["calls"] == r["tagged"] == 1 for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", [False, True])
async def test_fleet_cost_never_includes_private_worker_namespaces(
    reader: Any, operator: bool
) -> None:
    rows = await admin.fleet_cost(3600, 10, "namespace", operator=operator, ch=reader)
    assert len(rows) == 1 and rows[0]["key"] == "notes"
    assert float(rows[0]["total_cost_usd"]) == (13.25 if operator else 0.25)
    assert rows[0]["total_calls"] == (2 if operator else 1)
