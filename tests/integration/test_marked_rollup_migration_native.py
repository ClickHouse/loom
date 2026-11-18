"""The actual append refresh preserves old states while separating marked measures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.core.schema.journal import split_statements
from server.core.schema.migrate import definer_rewrite
from server.observability.events import AgentEvent
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")
MIGRATIONS = Path(__file__).resolve().parents[2] / "schemas" / "migrations"


class RollupWorld:
    def __init__(self, client: CHClient, database: str) -> None:
        self.client = client
        self.database = database
        self.org = unique_org("marked-migration")
        self.ts = datetime.now(UTC) - timedelta(minutes=2)

    def sql(self, statement: str) -> str:
        return definer_rewrite(statement).replace("loom.", self.database + ".")

    async def execute(self, statement: str) -> None:
        async with asyncio.timeout(15):
            await self.client.execute(self.sql(statement))

    async def insert(
        self, *, offset: int = 0, marked: bool = False, agent: str = "same", cost: float = 0.25
    ) -> None:
        row = AgentEvent(
            ts=self.ts + timedelta(seconds=offset),
            org=self.org,
            namespace="notes",
            agent=agent,
            user_id="reader",
            session_id="session",
            operation="memory.ask",
            request_id=uuid4().hex,
            latency_ms=999999 if marked else 11,
            success=marked,
            input_tokens=13 if marked else 3,
            output_tokens=7 if marked else 2,
            embedding_tokens=4 if marked else 1,
            upstream_cost_usd=cost,
            mirror_marked=marked,
        ).to_row()
        if not marked:
            row.pop("mirror_marked")  # An old writer does not send the added field.
        await self.client.insert(f"{self.database}.agent_events", [row], async_insert_settings())

    async def refresh(self) -> None:
        await self.execute("SYSTEM START VIEW loom.agent_cost_rollup_refresh")
        await self.execute("SYSTEM REFRESH VIEW loom.agent_cost_rollup_refresh")
        await self.execute("SYSTEM WAIT VIEW loom.agent_cost_rollup_refresh")
        await self.execute("SYSTEM PAUSE VIEW loom.agent_cost_rollup_refresh")
        await self.execute("SYSTEM WAIT VIEW loom.agent_cost_rollup_refresh")

    async def cutover(self) -> None:
        migration = MIGRATIONS / "106_marked_request_measures.sql"
        statements = split_statements(migration.read_text())
        for statement in statements:
            words = statement.split()
            is_cost_control = words[:3] in (
                ["SYSTEM", "PAUSE", "VIEW"],
                ["SYSTEM", "WAIT", "VIEW"],
                ["SYSTEM", "START", "VIEW"],
            ) and words[3:] == ["loom.agent_cost_rollup_refresh"]
            is_cost_alter = words[:2] == ["ALTER", "TABLE"] and words[2] in {
                "loom.agent_events",
                "loom.agent_cost_rollup_daily",
                "loom.agent_cost_rollup_refresh",
            }
            if is_cost_control or is_cost_alter:
                await self.execute(statement)

    async def states(self) -> list[dict]:
        return await self.client.query_all(
            self.sql("""
            SELECT agent, toFloat64(sumMerge(cost_usd_state)) AS ordinary_cost,
                   countMerge(ops_state) AS ordinary_ops,
                   sumMerge(tokens_state) AS ordinary_tokens,
                   sumMerge(embed_tokens_state) AS ordinary_embeddings,
                   sumMerge(errors_state) AS errors,
                   sumMerge(latency_sum_state) AS latency,
                   arrayAll(x -> isNaN(x),
                       quantilesMerge(0.5, 0.95, 0.99)(latency_quantile_state)) AS empty_latency,
                   toFloat64(sumMerge(marked_cost_usd_state)) AS marked_cost,
                   countMerge(marked_ops_state) AS marked_ops,
                   sumMerge(marked_tokens_state) AS marked_tokens,
                   sumMerge(marked_embed_tokens_state) AS marked_embeddings,
                   max(ts_hwm) AS hwm
            FROM loom.agent_cost_rollup_daily
            GROUP BY agent ORDER BY agent
            """)
        )


@pytest_asyncio.fixture
async def world() -> AsyncIterator[RollupWorld]:
    client = CHClient()
    database = "marked_migration_" + uuid4().hex
    database_uuid = str(uuid4())
    attempted = False
    try:
        await client.connect()
        attempted = True
        await client.execute(f"CREATE DATABASE {database} UUID '{database_uuid}' ENGINE = Atomic")
        world = RollupWorld(client, database)
        await client.execute(f"CREATE TABLE {database}.agent_events AS loom.agent_events")
        # This fixture replays the historical marker migration before the physical split.
        await client.execute(
            f"ALTER TABLE {database}.agent_events DROP CONSTRAINT IF EXISTS ordinary_events_only"
        )
        baseline = split_statements((MIGRATIONS / "016_baseline.sql").read_text())
        for name in ("agent_cost_rollup_daily", "agent_cost_rollup_refresh"):
            statement = next(
                sql
                for sql in baseline
                if sql.startswith(
                    (
                        "CREATE TABLE IF NOT EXISTS loom." + name + "\n",
                        "CREATE MATERIALIZED VIEW IF NOT EXISTS loom." + name + "\n",
                    )
                )
            )
            await world.execute(statement)
        await world.execute("SYSTEM WAIT VIEW loom.agent_cost_rollup_refresh")
        await world.execute("SYSTEM PAUSE VIEW loom.agent_cost_rollup_refresh")
        await world.execute("SYSTEM WAIT VIEW loom.agent_cost_rollup_refresh")
        yield world
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


async def test_old_parts_and_omitted_columns_keep_empty_marked_states(world: RollupWorld) -> None:
    await world.insert()
    await world.refresh()
    contract_sql = (
        "SELECT engine, sorting_key, primary_key, partition_key FROM system.tables "
        "WHERE database = {database:String} AND name = 'agent_cost_rollup_daily'"
    )
    contract = await world.client.query_one(contract_sql, params={"database": world.database})
    await world.cutover()
    assert (
        await world.client.query_one(contract_sql, params={"database": world.database}) == contract
    )
    columns = await world.client.query_all(
        "SELECT name, type FROM system.columns WHERE database = {database:String} "
        "AND table = 'agent_cost_rollup_daily'",
        params={"database": world.database},
    )
    types = {row["name"]: row["type"] for row in columns}
    for old, marked in (
        ("cost_usd_state", "marked_cost_usd_state"),
        ("tokens_state", "marked_tokens_state"),
        ("embed_tokens_state", "marked_embed_tokens_state"),
        ("ops_state", "marked_ops_state"),
    ):
        assert types[old] == types[marked]
    view_columns = await world.client.query_all(
        "SELECT name FROM system.columns WHERE database = {database:String} "
        "AND table = 'agent_cost_rollup_refresh'",
        params={"database": world.database},
    )
    assert {
        "marked_cost_usd_state",
        "marked_tokens_state",
        "marked_embed_tokens_state",
        "marked_ops_state",
    } <= {row["name"] for row in view_columns}
    first = (await world.states())[0]
    assert first["ordinary_ops"] == 1 and first["ordinary_cost"] == 0.25
    assert first["marked_ops"] == first["marked_cost"] == 0
    assert first["marked_tokens"] == first["marked_embeddings"] == 0
    await world.insert(offset=1)
    await world.refresh()
    after = (await world.states())[0]
    assert after["ordinary_ops"] == 2 and after["ordinary_cost"] == 0.5
    assert after["ordinary_tokens"] == 10 and after["ordinary_embeddings"] == 2
    assert after["errors"] == 2 and after["latency"] == 22
    assert after["marked_ops"] == after["marked_cost"] == 0
    await world.cutover()
    await world.refresh()
    assert await world.states() == [after]


async def test_same_key_old_and_marked_parts_merge_without_fake_samples(world: RollupWorld) -> None:
    await world.insert()
    await world.refresh()
    await world.cutover()
    await world.insert(offset=1, marked=True, cost=2)
    await world.insert(offset=1, marked=True, agent="marked-only", cost=3)
    await world.insert(offset=1, agent="zero-spend", cost=0)
    await world.refresh()
    before = await world.states()
    rows = {row["agent"]: row for row in before}
    shared = rows["same"]
    assert shared["ordinary_ops"] == 1 and shared["ordinary_cost"] == 0.25
    assert shared["ordinary_tokens"] == 5 and shared["ordinary_embeddings"] == 1
    assert shared["marked_ops"] == 1 and shared["marked_cost"] == 2
    assert shared["marked_tokens"] == 20 and shared["marked_embeddings"] == 4
    assert shared["errors"] == 1 and shared["latency"] == 11
    marked = rows["marked-only"]
    assert marked["ordinary_ops"] == marked["errors"] == marked["latency"] == 0
    assert marked["empty_latency"] and marked["marked_ops"] == 1 and marked["marked_cost"] == 3
    assert rows["zero-spend"]["ordinary_ops"] == 1
    assert rows["zero-spend"]["ordinary_cost"] == 0
    await world.execute("OPTIMIZE TABLE loom.agent_cost_rollup_daily FINAL")
    assert await world.states() == before
    await world.refresh()
    assert await world.states() == before


async def test_cutover_drains_a_running_append_without_cancelling_it(world: RollupWorld) -> None:
    row = await world.client.query_one(
        "SELECT as_select FROM system.tables WHERE database = {database:String} "
        "AND name = 'agent_cost_rollup_refresh'",
        params={"database": world.database},
    )
    assert row
    query = row["as_select"].replace(
        "FROM " + world.database + ".agent_events",
        "FROM (SELECT * FROM " + world.database + ".agent_events WHERE sleepEachRow(0.1) = 0)",
    )
    assert query != row["as_select"]
    await world.execute("ALTER TABLE loom.agent_cost_rollup_refresh MODIFY QUERY " + query)
    for offset in range(10):
        await world.insert(offset=offset)
    await world.execute("SYSTEM START VIEW loom.agent_cost_rollup_refresh")
    await world.execute("SYSTEM REFRESH VIEW loom.agent_cost_rollup_refresh")
    async with asyncio.timeout(10):
        while True:
            running = await world.client.query_one(
                "SELECT status FROM system.view_refreshes WHERE database = {database:String} "
                "AND view = 'agent_cost_rollup_refresh'",
                params={"database": world.database},
            )
            if running and running["status"] == "Running":
                break
            await asyncio.sleep(0.01)
    await world.cutover()
    ordinary = (await world.states())[0]
    assert ordinary["ordinary_ops"] == 10 and ordinary["ordinary_cost"] == 2.5
    assert ordinary["marked_ops"] == 0
    await world.insert(offset=10, marked=True, cost=2)
    await world.refresh()
    final = (await world.states())[0]
    assert final["ordinary_ops"] == 10 and final["ordinary_cost"] == 2.5
    assert final["marked_ops"] == 1 and final["marked_cost"] == 2
