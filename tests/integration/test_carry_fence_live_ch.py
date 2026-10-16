"""A carried batch held in transit past an ALTER is refused by the server (server/core/rmt_carry.py).

The batch is built and its request kept, a column is added and given a value by a
newer write, then the kept request is delivered: ClickHouse's guard refuses its
rows, so the value stays. Runs on a private table this test registers as carried.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import ch as ch_mod
from server.core import rmt_carry
from server.core.ch import CHClient

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.asyncio
async def test_a_batch_delivered_after_an_alter_keeps_the_new_columns_value() -> None:
    table = f"loom.carry_fence_{uuid.uuid4().hex[:12]}"
    c = CHClient()
    await c.connect()
    try:
        await c.execute(
            f"CREATE TABLE {table} (k String, v String, version UInt64) "
            "ENGINE = ReplacingMergeTree(version) ORDER BY k"
        )
        rmt_carry.register(table, key=("k",), columns=("k", "v", "version"))
        await c.insert(table, [{"k": "a", "v": "first", "version": 1}])  # guarded, lands

        held: dict[str, Any] = {}
        deliver = c.client.data_insert

        async def keep(ctx: Any) -> Any:  # the request is held in transit
            held["ctx"] = ctx
            return SimpleNamespace(written_rows=1)

        c.client.data_insert = keep
        await c.insert(table, [{"k": "a", "v": "stale", "version": 9}])
        c.client.data_insert = deliver

        await c.execute(
            f"ALTER TABLE {table} ADD COLUMN new_state String DEFAULT '' "
            "SETTINGS mutations_sync = 2"
        )
        await c.execute(f"INSERT INTO {table} VALUES ('a', 'newer', 5, 'preserve-me')")
        with pytest.raises(Exception, match=ch_mod.FENCE_MESSAGE):
            await deliver(held["ctx"])
        rows = await c.query_all(f"SELECT v, new_state FROM {table} FINAL")
        assert rows == [{"v": "newer", "new_state": "preserve-me"}]
    finally:
        rmt_carry._TABLES.pop(table, None)
        ch_mod.describe_cache_clear()
        await c.execute(f"DROP TABLE IF EXISTS {table} SYNC")
        await c.disconnect()


@pytest.mark.asyncio
async def test_a_batch_delivered_after_its_start_deadline_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still in transit when its writer was released: it may not start late."""
    import asyncio

    monkeypatch.setattr(rmt_carry, "_TRANSIT_S", 1.0)
    table = f"loom.carry_late_{uuid.uuid4().hex[:12]}"
    c = CHClient()
    await c.connect()
    try:
        await c.execute(
            f"CREATE TABLE {table} (k String, v String, version UInt64) "
            "ENGINE = ReplacingMergeTree(version) ORDER BY k"
        )
        rmt_carry.register(table, key=("k",), columns=("k", "v", "version"))
        held: dict[str, Any] = {}
        deliver = c.client.data_insert

        async def keep(ctx: Any) -> Any:
            held["ctx"] = ctx
            return SimpleNamespace(written_rows=1)

        c.client.data_insert = keep
        await c.insert(table, [{"k": "a", "v": "late", "version": 9}])
        c.client.data_insert = deliver
        await asyncio.sleep(2.0)  # past its deadline
        with pytest.raises(Exception, match=ch_mod.DEADLINE_MESSAGE):
            await deliver(held["ctx"])
        assert await c.query_all(f"SELECT k FROM {table}") == []
    finally:
        rmt_carry._TABLES.pop(table, None)
        ch_mod.describe_cache_clear()
        await c.execute(f"DROP TABLE IF EXISTS {table} SYNC")
        await c.disconnect()
