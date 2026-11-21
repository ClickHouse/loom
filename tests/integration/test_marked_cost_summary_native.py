"""Marked spend appears only in authorized totals, across stored and live measures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio

from server.auth import context
from server.auth.jwt import AuthInfo
from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.insights.api.models_cost import CostSummaryInput
from server.insights.app import cost as handlers
from server.insights.store import cost
from server.observability.budget import budget_snapshot
from server.observability.events import AgentEvent
from server.operations.base import Role
from tests.integration._event_fixtures import insert_event_rows
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


class CostWorld:
    """A private copy of the actual migrated tables, without a background refresh."""

    def __init__(self, client: CHClient, database: str) -> None:
        self.client = client
        self.database = database
        self.org = unique_org("marked-cost")
        self.ts = (datetime.now(UTC) - timedelta(minutes=2)).replace(second=0, microsecond=0)
        self.reader = SimpleNamespace(query_all=self.query_all, query_one=self.query_one)

    def sql(self, sql: str) -> str:
        return sql.replace(
            "loom.agent_cost_rollup_daily", f"{self.database}.agent_cost_rollup_daily"
        ).replace("loom.agent_events", f"{self.database}.agent_events")

    async def query_all(self, sql: str, params: Any = None) -> Any:
        return await self.client.query_all(self.sql(sql), params=params)

    async def query_one(self, sql: str, params: Any = None) -> Any:
        return await self.client.query_one(self.sql(sql), params=params)

    async def insert(self, *, tail: bool = False) -> None:
        ordinary = AgentEvent(
            ts=self.ts + timedelta(seconds=int(tail)),
            org=self.org,
            namespace="notes",
            agent="shared",
            user_id="reader",
            session_id="session",
            operation="memory.ask",
            request_id=uuid4().hex,
            latency_ms=11,
            success=False,
            input_tokens=3,
            output_tokens=2,
            embedding_tokens=1,
            upstream_cost_usd=0.25,
            model="ordinary",
            args={"out_llm_cache_hits": "2", "out_llm_cache_saved_usd": "0.4"},
        ).to_row()
        marked = {
            **ordinary,
            "request_id": uuid4().hex,
            "mirror_marked": 1,
            "input_tokens": 13,
            "output_tokens": 7,
            "embedding_tokens": 4,
            "upstream_cost_usd": 2,
            "latency_ms": 999999,
            "model": "private",
            "args": {"out_llm_cache_hits": "99", "out_llm_cache_saved_usd": "77"},
        }
        only_marked = {
            **marked,
            "agent": "marked-only",
            "operation": "memory.get",
            "upstream_cost_usd": 3,
        }
        zero_spend = {
            **ordinary,
            "agent": "zero",
            "operation": "memory.get",
            "upstream_cost_usd": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "embedding_tokens": 0,
            "success": True,
            "latency_ms": 13,
            "args": {},
        }
        await insert_event_rows(
            self.client,
            f"{self.database}.agent_events",
            [
                ordinary,
                marked,
                only_marked,
                zero_spend,
                {**marked, "org": self.org + "-foreign", "upstream_cost_usd": 900},
                {**marked, "namespace": "other", "upstream_cost_usd": 5},
            ],
            async_insert_settings(),
            required_columns={"mirror_marked"},
        )

    async def fold(self) -> None:
        row = await self.client.query_one(
            "SELECT as_select FROM system.tables "
            "WHERE database = 'loom' AND name = 'agent_cost_rollup_refresh'"
        )
        assert row and "mirror_marked" in row["as_select"], "marked refresh schema prerequisite"
        await self.client.execute(
            f"INSERT INTO {self.database}.agent_cost_rollup_daily "
            f"({cost.ROLLUP_DIMS}, {cost.ROLLUP_STATES}, ts_hwm) " + self.sql(row["as_select"])
        )

    async def summary(self, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
        monkeypatch.setattr(handlers, "get_client", lambda: self.reader)
        return await handlers.handle_cost_summary(
            CostSummaryInput(
                org=self.org,
                from_date=self.ts.date(),
                to_date=self.ts.date(),
                **{"namespace": "notes", **kwargs},
            )
        )


@pytest_asyncio.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[CostWorld]:
    client = CHClient()
    database = "marked_cost_" + uuid4().hex
    database_uuid = str(uuid4())
    attempted = False
    try:
        await client.connect()
        columns = await client.query_all("DESCRIBE TABLE loom.agent_events")
        assert "mirror_marked" in {r["name"] for r in columns}, "marked event schema prerequisite"
        columns = await client.query_all("DESCRIBE TABLE loom.agent_cost_rollup_daily")
        assert {
            "marked_cost_usd_state",
            "marked_tokens_state",
            "marked_embed_tokens_state",
            "marked_ops_state",
        } <= {r["name"] for r in columns}, "marked rollup schema prerequisite"
        attempted = True
        await client.execute(f"CREATE DATABASE {database} UUID '{database_uuid}' ENGINE = Atomic")
        await client.execute(f"CREATE TABLE {database}.agent_events AS loom.agent_events")
        await client.execute(
            f"CREATE TABLE {database}.agent_events_private AS loom.agent_events_private"
        )
        await client.execute(
            f"CREATE TABLE {database}.agent_cost_rollup_daily AS loom.agent_cost_rollup_daily"
        )
        monkeypatch.setattr(context, "current_auth", lambda: None)
        yield CostWorld(client, database)
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
@pytest.mark.parametrize("folded", [False, True])
async def test_namespace_day_totals_merge_stored_and_live_marked_spend_once(
    world: CostWorld, monkeypatch: pytest.MonkeyPatch, folded: bool
) -> None:
    await world.insert()
    if folded:
        await world.fold()
    await world.insert(tail=True)
    out = await world.summary(monkeypatch, group_by=["namespace", "day"])
    assert out.total_ops == 8 and out.total_errors == 2
    assert out.total_cost_usd == 10.5
    assert out.rows[0].tokens == 90 and out.rows[0].embed_tokens == 18
    assert out.cache_hits == 4 and out.cache_saved_usd == 0.8
    assert out.rows[0].latency_p99_ms <= 13


@pytest.mark.asyncio
@pytest.mark.parametrize("folded", [False, True])
@pytest.mark.parametrize("group_by", [["agent"], ["operation"], ["model"]])
async def test_fine_groups_keep_ordinary_zero_spend_but_no_marked_only_group(
    world: CostWorld, monkeypatch: pytest.MonkeyPatch, folded: bool, group_by: list[str]
) -> None:
    await world.insert()
    if folded:
        await world.fold()
    out = await world.summary(monkeypatch, group_by=group_by)
    assert out.total_ops == 2 and out.total_errors == 1 and out.total_cost_usd == 0.25
    assert sum(r.tokens for r in out.rows) == 5
    assert all(r.latency_p99_ms <= 13 for r in out.rows)
    assert all(r.agent != "marked-only" and r.model != "private" for r in out.rows)
    if group_by == ["agent"]:
        assert {r.agent for r in out.rows} == {"shared", "zero"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [
        {"group_by": []},
        {"group_by": ["namespace"], "to_date": "later"},
        {"namespace": None, "group_by": ["day"]},
        {"group_by": ["day"], "agent": "shared"},
        {"group_by": ["day"], "operation": "memory.ask"},
        {"group_by": ["day"], "user_id": "reader"},
    ],
)
async def test_coarse_or_filtered_selections_exclude_marked_spend(
    world: CostWorld, monkeypatch: pytest.MonkeyPatch, selection: dict[str, Any]
) -> None:
    await world.insert()
    await world.fold()
    selection = dict(selection)
    if selection.pop("to_date", None) == "later":
        # The handler's public input accepts dates, never raw timestamp windows.
        monkeypatch.setattr(handlers, "get_client", lambda: world.reader)
        out = await handlers.handle_cost_summary(
            CostSummaryInput(
                org=world.org,
                namespace="notes",
                from_date=world.ts.date(),
                to_date=world.ts.date() + timedelta(days=1),
                **selection,
            )
        )
    else:
        out = await world.summary(monkeypatch, **selection)
    assert out.total_cost_usd == 0.25
    assert out.total_errors == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_only_a_flat_platform_admin_sees_marked_fine_spend(
    world: CostWorld, monkeypatch: pytest.MonkeyPatch, scoped: bool
) -> None:
    await world.insert()
    await world.fold()
    auth = AuthInfo(
        subject="operator",
        org=world.org,
        namespaces=("notes",),
        roles=(Role.PLATFORM_ADMIN,),
        is_service_account=False,
        ns_roles=(("notes", "admin"),) if scoped else (),
    )
    monkeypatch.setattr(context, "current_auth", lambda: auth)
    out = await world.summary(monkeypatch, group_by=["agent"])
    assert out.total_cost_usd == (0.25 if scoped else 5.25)
    assert out.total_ops == (2 if scoped else 4)
    assert out.total_errors == 1


@pytest.mark.asyncio
async def test_marked_only_totals_have_no_latency_or_cache_samples(
    world: CostWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.insert()
    await world.fold()
    out = await world.summary(monkeypatch, namespace="other", group_by=["namespace", "day"])
    assert out.total_cost_usd == 5 and out.total_ops == 1 and out.total_errors == 0
    assert out.cache_hits == 0 and out.cache_saved_usd == 0
    assert out.rows[0].latency_p50_ms == out.rows[0].latency_p99_ms == 0
    snapshot = await budget_snapshot(world.org, "notes", read_uncapped=True, ch=world.reader)
    assert snapshot.spent == 5.25
    assert await cost.org_spend_24h(world.org, "", {}, ch=world.reader) == (10.25, 2)
