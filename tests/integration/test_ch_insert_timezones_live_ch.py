"""A naive UTC datetime written through `CHClient.insert` is stored as that UTC
instant whatever the client host's time zone, and reads back as it was written;
a handler that stamps a row with the time stores the present. Each zone runs in
its own process: the driver reads the local zone at import.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

ROOT = Path(__file__).resolve().parents[2]
WRITTEN = "2026-01-02 03:04:05.678"
EPOCH_MS = 1767323045678  # WRITTEN as UTC

ROUND_TRIP = """
import asyncio, sys
from datetime import datetime
from server.core.ch import get_client

async def main(table: str) -> None:
    ch = get_client()
    await ch.connect()
    written = datetime.fromisoformat(sys.argv[2])
    await ch.insert(table, [{"t": written, "t64": written, "hours": [written]}])
    row = await ch.query_one(
        f"SELECT toUnixTimestamp64Milli(t64) AS ms, toUnixTimestamp(t) AS s, "
        f"toUnixTimestamp(hours[1]) AS h, t64 FROM {table}"
    )
    print(row["ms"], row["s"], row["h"], row["t64"] == written)
    await ch.disconnect()

asyncio.run(main(sys.argv[1]))
"""


CONVERSATION = """
import asyncio, sys, uuid
from server.core.ch import get_client
from server.knowledge.api.models_conversations import ConversationCreateInput
from server.knowledge.app.conversations import handle_conversation_create

async def main(org: str) -> None:
    ch = get_client()
    await ch.connect()
    thread = uuid.uuid4().hex
    await handle_conversation_create(
        ConversationCreateInput(org=org, namespace="ns", agent="a", thread_id=thread)
    )
    row = await ch.query_one(
        "SELECT dateDiff('second', created_at, now()) AS lag FROM loom.conversation_threads "
        "WHERE org = {org:String} AND thread_id = {t:String}",
        params={"org": org, "t": thread},
    )
    print(row["lag"])
    await ch.disconnect()

asyncio.run(main(sys.argv[1]))
"""

LEASE_RENEWAL = """
import sys
from server.core.schema.migrate import client
from server.core.schema.migrate_lease import LEASE_NAME, LEASE_TABLE, LeaseConfig, MigrationLease

ch = client()
lease = MigrationLease(ch, cfg=LeaseConfig(settle_s=0, wait_s=5), holder=sys.argv[1], out=lambda _: None)
lease.acquire()
first = lease._acquired_at
lease.release()  # re-inserts the row with the acquired_at it read back
row = ch.query(
    f"SELECT acquired_at FROM {LEASE_TABLE} FINAL WHERE name = {{n:String}} AND holder = {{h:String}}",
    parameters={"n": LEASE_NAME, "h": sys.argv[1]},
).result_rows[0]
print(first is not None and row[0] == first)
ch.command(
    f"DELETE FROM {LEASE_TABLE} WHERE holder = {{h:String}}", parameters={"h": sys.argv[1]}
)
"""

ZONES = ["America/Los_Angeles", "UTC", "Asia/Kolkata"]


async def _run_in(zone: str, script: str, *args: str) -> str:
    env = {**os.environ, "TZ": zone, "PYTHONPATH": str(ROOT)}
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, *args],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


@pytest.fixture
async def table() -> Any:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    name = f"loom._tz_round_trip_{uuid.uuid4().hex[:12]}"
    await ch.execute(
        f"CREATE TABLE {name} (t DateTime, t64 DateTime64(3), hours Array(DateTime)) "
        "ENGINE = MergeTree ORDER BY tuple()"
    )
    try:
        yield name
    finally:
        await ch.execute(f"DROP TABLE IF EXISTS {name}")
        await ch.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ZONES)
async def test_a_naive_utc_write_keeps_its_instant_in_any_host_zone(table: str, zone: str) -> None:
    out = await _run_in(zone, ROUND_TRIP, table, WRITTEN)
    ms, s, h, read_back = out.split()[-4:]
    assert (int(ms), int(s), int(h)) == (EPOCH_MS, EPOCH_MS // 1000, EPOCH_MS // 1000)
    assert read_back == "True"


@pytest.fixture
async def org() -> Any:
    from server.core.ch import get_client

    name = f"itest-tz-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        ch = get_client()
        await ch.connect()
        await ch.execute(
            "DELETE FROM loom.conversation_threads WHERE org = {org:String}", params={"org": name}
        )
        await ch.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ZONES)
async def test_a_thread_is_stored_created_now_in_any_host_zone(org: str, zone: str) -> None:
    lag = int((await _run_in(zone, CONVERSATION, org)).split()[-1])
    assert abs(lag) < 60  # an offset would be hours


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", ZONES)
async def test_a_lease_renewal_keeps_its_queue_position_in_any_host_zone(zone: str) -> None:
    out = await _run_in(zone, LEASE_RENEWAL, f"tz-test-{uuid.uuid4().hex[:8]}")
    assert out.split()[-1] == "True"
