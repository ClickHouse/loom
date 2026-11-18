"""Warehouse usage folds marked spend before name hashing and result predicates."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.insights.store import analytics_export
from server.observability.events import AgentEvent
from tests.integration._event_fixtures import insert_event_rows

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest_asyncio.fixture
async def reader() -> AsyncIterator[Any]:
    client = CHClient()
    database = "export_marked_" + uuid4().hex
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

        async def query(sql: str, params: Any) -> Any:
            return await client.query_all(
                sql.replace("loom.agent_events", f"{database}.agent_events"), params=params
            )

        yield SimpleNamespace(client=client, table=f"{database}.agent_events", query=query)
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


async def populate(reader: Any, *, success: bool) -> Any:
    day = datetime.now(UTC).date() - timedelta(days=1)
    stamp = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=12)
    ordinary = AgentEvent(
        ts=stamp,
        org="example",
        namespace="notes",
        agent="ordinary",
        user_id="reader",
        session_id="session",
        operation="memory.ask",
        request_id=uuid4().hex,
        latency_ms=7,
        success=True,
        upstream_cost_usd=0.25,
        input_tokens=3,
        output_tokens=2,
        embedding_tokens=4,
        llm_calls=1,
        bytes_written=9,
        kind="request",
        transport="http",
        traffic_class="foreground",
        slo_class="interactive",
        status_class="ok",
        budget_ms=10,
    ).to_row()
    marked = {
        **ordinary,
        "mirror_marked": 1,
        "agent": "private-left" if success else "private-right",
        "operation": "private.allow" if success else "private.deny",
        "kind": "request" if success else "worker",
        "transport": "http" if success else "mcp",
        "traffic_class": "foreground" if success else "private",
        "success": success,
        "status_class": "ok" if success else "server",
        "latency_ms": 13 if success else 997,
        "llm_calls": 13 if success else 997,
        "bytes_written": 13 if success else 997,
        "upstream_cost_usd": 13,
        "input_tokens": 5,
        "output_tokens": 7,
        "embedding_tokens": 11,
    }
    # An ordinary placeholder key must retain its own measurements when the fold joins it.
    collision = {
        **ordinary,
        **{key: "-" for key in ("agent", "operation", "kind", "transport", "traffic_class")},
    }
    await insert_event_rows(
        reader.client,
        reader.table,
        [
            ordinary,
            collision,
            marked,
            {**marked, "agent": "another-private", "operation": "another.private"},
            {**marked, "namespace": "acl-" + "a" * 24},
            {**marked, "ts": stamp - timedelta(days=1)},
        ],
        async_insert_settings(),
        required_columns={"mirror_marked"},
    )
    return day


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_marked_spend_folds_without_work_or_fine_dimensions(
    reader: Any, success: bool
) -> None:
    day = await populate(reader, success=success)
    rows = await reader.query(analytics_export.USAGE_SQL, {"day": day})
    assert len(rows) == 2
    ordinary = next(r for r in rows if r["agent"] == "ordinary")
    assert ordinary["operation"] == "memory.ask"
    assert ordinary["ops"] == ordinary["users"] == ordinary["sessions"] == 1
    assert ordinary["llm_calls"] == 1 and ordinary["bytes_written"] == 9
    assert ordinary["latency_p50_ms"] == ordinary["latency_p99_ms"] == 7
    folded = next(r for r in rows if r["agent"] == "-")
    assert all(folded[key] == "-" for key in ("operation", "kind", "transport", "traffic_class"))
    assert folded["namespace"] == "notes" and folded["ops"] == 3
    assert float(folded["cost_usd"]) == 26.25
    assert folded["input_tokens"] == 13 and folded["output_tokens"] == 16
    assert folded["embedding_tokens"] == 26
    assert folded["llm_calls"] == folded["users"] == folded["sessions"] == 1
    assert folded["bytes_written"] == 9
    assert folded["errors"] == folded["server_errors"] == folded["slow"] == 0
    assert folded["latency_p50_ms"] == folded["latency_p95_ms"] == folded["latency_p99_ms"] == 0
    # A caller filter can select only the folded placeholder, never a private partition.
    for column in ("agent", "operation"):
        assert (
            await reader.query(
                f"SELECT * FROM ({analytics_export.USAGE_SQL}) WHERE {column} = {{private:String}}",
                {"day": day, "private": "private-left" if column == "agent" else "private.allow"},
            )
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [False, True])
async def test_export_hashes_only_after_marked_dimensions_collapse(
    reader: Any, success: bool
) -> None:
    day = await populate(reader, success=success)
    rows = await reader.query(analytics_export._hashed("usage"), {"day": day, "k0": 1, "k1": 2})
    assert len(rows) == 2
    assert sum(r["ops"] for r in rows) == 4
    assert sum(float(r["cost_usd"]) for r in rows) == 26.5
    assert all(len(r[name]) == 32 for r in rows for name in ("org", "namespace", "agent"))
    assert {r["operation"] for r in rows} == {"memory.ask", "-"}
    assert [tuple(r[c] for c in analytics_export.USAGE_KEY.split(", ")) for r in rows] == sorted(
        tuple(r[c] for c in analytics_export.USAGE_KEY.split(", ")) for r in rows
    )
