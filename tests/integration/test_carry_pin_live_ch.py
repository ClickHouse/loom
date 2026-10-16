"""A carried write cut off after its guard passed holds the schema barrier until the
server says its INSERT ended (server/core/rmt_carry.py).

The INSERT is made slow on the server (a MATERIALIZED column that sleeps per row),
its caller cancelled while it runs, and a column added meanwhile: no newer lists
are acknowledged until no replica runs the query, so a new build that writes the
column after the barrier keeps its value. Runs on a private table registered as
carried.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

import pytest

from server.core import ch as ch_mod
from server.core import rmt_carry
from server.core.ch import CHClient

pytestmark = pytest.mark.usefixtures("ch_target_required")


async def _running(c: CHClient, table: str) -> int:
    row = await c.query_one(
        "SELECT count() AS n FROM system.processes "
        "WHERE query LIKE '%input(%' AND position(query, {t:String}) > 0 "
        "AND query_id != queryID()",
        {"t": table},
    )
    return int(row["n"]) if row else 0


@pytest.mark.asyncio
async def test_a_write_cut_off_after_its_guard_holds_the_barrier_until_it_ends() -> None:
    table = f"loom.carry_pin_{uuid.uuid4().hex[:12]}"
    c = CHClient()
    await c.connect()
    try:
        await c.execute(
            f"CREATE TABLE {table} (k String, v String, version UInt64, "
            "slow UInt8 MATERIALIZED sleepEachRow(1)) "
            "ENGINE = ReplacingMergeTree(version) ORDER BY k"
        )
        rmt_carry.register(table, key=("k",), columns=("k", "v", "version"))
        await c.insert(table, [{"k": "a", "v": "first", "version": 1}])
        old = rmt_carry.acknowledged()[table]

        stale = [{"k": k, "v": "stale", "version": 9} for k in ("a", "b", "c")]  # ~3 s
        write = asyncio.create_task(c.insert(table, stale))
        deadline = time.monotonic() + 10
        while not await _running(c, table):
            assert time.monotonic() < deadline, "the slow INSERT never started"
            await asyncio.sleep(0.05)
        write.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await write
        assert rmt_carry._pinned, "a write cut off after its send must stay in flight"

        await c.execute(f"ALTER TABLE {table} ADD COLUMN new_state String DEFAULT ''")
        await rmt_carry._read_columns(c)  # what the watcher does after the ALTER
        assert rmt_carry.acknowledged()[table] == old  # still the lists the INSERT took

        deadline = time.monotonic() + 20
        while rmt_carry._pinned:
            assert time.monotonic() < deadline, "the cut-off write was never settled"
            await rmt_carry._settle(c)
            await asyncio.sleep(0.2)
        assert await _running(c, table) == 0
        assert rmt_carry.acknowledged()[table] != old

        # The new build, past the barrier: one version above what it reads.
        row = await c.query_one(f"SELECT max(version) AS v FROM {table} FINAL WHERE k = 'a'")
        newer = int(row["v"]) + 1 if row else 2
        await c.execute(
            f"INSERT INTO {table} (k, v, version, new_state) "
            f"VALUES ('a', 'newer', {newer}, 'preserve-me')"
        )
        await asyncio.sleep(3)  # nothing of the old INSERT lands after this
        got = await c.query_one(f"SELECT new_state FROM {table} FINAL WHERE k = 'a'")
        assert got == {"new_state": "preserve-me"}
    finally:
        rmt_carry._TABLES.pop(table, None)
        rmt_carry._pinned.clear()
        rmt_carry._inflight.clear()
        ch_mod.describe_cache_clear()
        await c.execute(f"DROP TABLE IF EXISTS {table} SYNC")
        await c.disconnect()
