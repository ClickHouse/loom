"""Durable refresh SQL keeps private measurements out while advancing its watermark."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.observability.events import AgentEvent
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


class FoldWorld:
    def __init__(self, client: CHClient, database: str, stamp: datetime) -> None:
        self.client = client
        self.database = database
        self.stamp = stamp
        self.org = unique_org("marked-fold")

    def sql(self, statement: str) -> str:
        for source in ("loom_telemetry.sli_minute", "loom.subject_last_seen", "loom.agent_events"):
            statement = statement.replace(source, self.database + "." + source.split(".")[1])
        return statement

    async def insert(
        self,
        *,
        offset: int = 0,
        marked: bool = False,
        namespace: str = "notes",
        subject: str = "reader",
        **extra: Any,
    ) -> None:
        row = AgentEvent(
            ts=self.stamp + timedelta(seconds=offset),
            org=self.org,
            namespace=namespace,
            agent="agent",
            user_id="reader",
            session_id="session",
            subject=subject,
            operation="memory.get",
            request_id=uuid4().hex,
            latency_ms=7,
            success=True,
            kind="request",
            transport="http",
            traffic_class="foreground",
            slo_class="read",
            status_class="ok",
            budget_ms=10,
            mirror_marked=marked,
        ).to_row()
        row.update(ingested_at=self.stamp + timedelta(seconds=offset), **extra)
        await self.client.insert(
            f"{self.database}.agent_events",
            [row],
            async_insert_settings(),
            required_columns={"mirror_marked"},
        )

    async def fold(self, table: str) -> None:
        database = "loom_telemetry" if table == "sli_minute" else "loom"
        row = await self.client.query_one(
            "SELECT as_select FROM system.tables "
            "WHERE database = {database:String} AND name = {name:String}",
            params={"database": database, "name": table + "_refresh"},
        )
        assert row and "namespace" in row["as_select"], "private refresh schema prerequisite"
        await self.client.execute(
            f"INSERT INTO {self.database}.{table} " + self.sql(row["as_select"])
        )

    async def sli(self) -> dict[str, Any]:
        row = await self.client.query_one(
            f"SELECT sum(requests) AS requests, sum(server_errors) AS server_errors, "
            f"sum(client_errors) AS client_errors, sum(slow) AS slow, "
            f"quantilesTDigestMerge(0.5, 0.95, 0.99)(latency) AS latency "
            f"FROM {self.database}.sli_minute WHERE slo_class != ''"
        )
        assert row
        return row


@pytest_asyncio.fixture
async def world() -> AsyncIterator[FoldWorld]:
    client = CHClient()
    database, database_uuid = "marked_fold_" + uuid4().hex, str(uuid4())
    attempted = False
    try:
        await client.connect()
        attempted = True
        await client.execute(f"CREATE DATABASE {database} UUID '{database_uuid}' ENGINE = Atomic")
        for source in ("loom.agent_events", "loom.subject_last_seen", "loom_telemetry.sli_minute"):
            await client.execute(f"CREATE TABLE {database}.{source.split('.')[1]} AS {source}")
        row = await client.query_one("SELECT now64(3) AS stamp")
        assert row
        stamp = row["stamp"].replace(tzinfo=UTC, microsecond=0) - timedelta(minutes=5)
        yield FoldWorld(client, database, stamp)
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


@pytest.mark.parametrize("private_success", [False, True])
async def test_sli_folds_only_ordinary_measurements_and_never_replays_them(
    world: FoldWorld, private_success: bool
) -> None:
    await world.insert()
    await world.insert(offset=1, success=False, status_class="server", latency_ms=900)
    await world.insert(
        offset=2,
        marked=True,
        success=private_success,
        status_class="ok" if private_success else "server",
        latency_ms=999999,
    )
    await world.insert(offset=3, namespace="acl-" + "a" * 24, latency_ms=999999)
    await world.fold("sli_minute")
    expected = {
        "requests": 2,
        "server_errors": 1,
        "client_errors": 0,
        "slow": 0,
        "latency": [7] * 3,
    }
    assert await world.sli() == expected
    await world.fold("sli_minute")
    assert await world.sli() == expected


async def test_a_private_only_sli_batch_advances_its_mark_without_a_measurement(
    world: FoldWorld,
) -> None:
    await world.insert(marked=True, latency_ms=999999)
    await world.insert(offset=1, namespace="acl-" + "a" * 24)
    await world.fold("sli_minute")
    row = await world.client.query_one(
        f"SELECT countIf(slo_class != '') AS data_rows, max(ingested_hwm) AS mark "
        f"FROM {world.database}.sli_minute"
    )
    assert row and row["data_rows"] == 0
    assert row["mark"].replace(tzinfo=UTC) >= world.stamp + timedelta(seconds=1)
    await world.fold("sli_minute")
    assert (await world.sli())["requests"] == 0


async def test_subject_fold_keeps_marked_participation_and_skips_private_workers(
    world: FoldWorld,
) -> None:
    await world.insert(subject="ordinary")
    await world.insert(offset=1, marked=True, subject="marked")
    await world.insert(offset=2, namespace="acl-" + "a" * 24, subject="worker")
    await world.fold("subject_last_seen")
    sql = (
        f"SELECT subject, maxMerge(last_seen) AS stamp FROM {world.database}.subject_last_seen "
        "WHERE subject != '' GROUP BY subject ORDER BY subject"
    )
    rows = await world.client.query_all(sql)
    assert {r["subject"] for r in rows} == {"ordinary", "marked"}
    times = {r["subject"]: r["stamp"].replace(tzinfo=UTC) for r in rows}
    assert times == {"ordinary": world.stamp, "marked": world.stamp + timedelta(seconds=1)}
    mark = await world.client.query_one(
        f"SELECT max(ts_hwm) AS stamp FROM {world.database}.subject_last_seen WHERE subject = ''"
    )
    assert mark and mark["stamp"].replace(tzinfo=UTC) == world.stamp + timedelta(seconds=2)
    await world.fold("subject_last_seen")
    assert await world.client.query_all(sql) == rows
    await world.insert(offset=3, subject="ordinary")
    await world.fold("subject_last_seen")
    after = await world.client.query_all(sql)
    assert {r["subject"] for r in after} == {"ordinary", "marked"}
    assert next(r["stamp"] for r in after if r["subject"] == "ordinary").replace(
        tzinfo=UTC
    ) == world.stamp + timedelta(seconds=3)
