"""A writer killed with a carried INSERT running holds the schema barrier until the
server says that INSERT ended (server/core/rmt_carry.py `await_acks`).

The killed process keeps no pin and its acknowledgement ages out after 30 s, but
the INSERT runs on: here 40 rows of a MATERIALIZED column that sleeps per row. A
boot that passed the barrier at the 30 s mark would let a newer write land and
then lose its new column to the old INSERT. Runs on a private table.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid

import pytest

from server.core import ch as ch_mod
from server.core import rmt_carry
from server.core.ch import CHClient

pytestmark = pytest.mark.usefixtures("ch_target_required")

_WRITER = """
import asyncio, sys
from server.core import rmt_carry
from server.core.ch import CHClient

async def main(table):
    c = CHClient()
    await c.connect()
    rmt_carry.register(table, key=("k",), columns=("k", "v", "version"))
    rows = [{"k": "a" if i == 0 else f"r{i}", "v": "stale", "version": 9} for i in range(40)]
    await c.insert(table, rows, {"function_sleep_max_microseconds_per_block": 60_000_000})

asyncio.run(main(sys.argv[1]))
"""


async def _running(c: CHClient, table: str) -> int:
    row = await c.query_one(
        "SELECT count() AS n FROM system.processes "
        "WHERE query_kind = 'Insert' AND startsWith(query_id, {p:String}) "
        "AND position(query, {t:String}) > 0",
        {"p": rmt_carry.CARRY_QUERY_PREFIX, "t": table},
    )
    return int(row["n"]) if row else 0


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrew", [False, True])
async def test_a_killed_writers_insert_holds_the_barrier_until_it_ends(withdrew: bool) -> None:
    """`withdrew`: its last acknowledgement says it carries nothing ("{}", live),
    as a shutdown that withdrew with the INSERT still running would leave."""
    table = f"loom.carry_dead_{uuid.uuid4().hex[:12]}"
    c = CHClient()
    await c.connect()
    writer: subprocess.Popen[bytes] | None = None
    try:
        await c.execute(
            f"CREATE TABLE {table} (k String, v String, version UInt64, "
            "slow UInt8 MATERIALIZED sleepEachRow(1)) "
            "ENGINE = ReplacingMergeTree(version) ORDER BY k"
        )
        writer = subprocess.Popen(  # noqa: ASYNC220 — killed by pid, never awaited
            [sys.executable, "-c", _WRITER, table], env=os.environ.copy()
        )
        deadline = time.monotonic() + 30
        while not await _running(c, table):
            assert writer.poll() is None, "the writer exited before its INSERT started"
            assert time.monotonic() < deadline, "the slow INSERT never started"
            await asyncio.sleep(0.1)
        started = time.monotonic()
        os.kill(writer.pid, signal.SIGKILL)
        await asyncio.to_thread(writer.wait)
        if withdrew:
            process = f"{socket.gethostname()[:64]}:{writer.pid}"
            await c.insert(rmt_carry.ACK_TABLE, [{"process": process, "fingerprints": "{}"}])

        await c.execute(f"ALTER TABLE {table} ADD COLUMN new_state String DEFAULT ''")
        rmt_carry.register(table, key=("k",), columns=("k", "v", "version", "new_state"))
        await rmt_carry.await_acks(c, timeout_s=120)
        assert await _running(c, table) == 0, "the barrier passed the running INSERT"
        if not withdrew:
            assert time.monotonic() - started > rmt_carry._ACK_LIVE_S  # past the ack's life

        # The new build, past the barrier: one version above what it reads.
        row = await c.query_one(f"SELECT max(version) AS v FROM {table} FINAL WHERE k = 'a'")
        newer = int(row["v"]) + 1 if row else 10
        await c.execute(
            f"INSERT INTO {table} (k, v, version, new_state) "
            f"VALUES ('a', 'newer', {newer}, 'preserve-me')"
        )
        got = await c.query_one(f"SELECT new_state FROM {table} FINAL WHERE k = 'a'")
        assert got == {"new_state": "preserve-me"}
    finally:
        if writer is not None and writer.poll() is None:
            os.kill(writer.pid, signal.SIGKILL)
        rmt_carry._TABLES.pop(table, None)
        ch_mod.describe_cache_clear()
        await c.execute(f"KILL QUERY WHERE position(query, '{table}') > 0 SYNC")
        await c.execute(f"DROP TABLE IF EXISTS {table} SYNC")
        await c.disconnect()
