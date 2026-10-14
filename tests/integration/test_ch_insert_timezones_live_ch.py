"""A naive UTC datetime written through `CHClient.insert` is stored as that UTC
instant whatever the client host's time zone, and reads back as it was written.
Each zone runs in its own process: the driver reads the local zone at import.

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
@pytest.mark.parametrize("zone", ["America/Los_Angeles", "UTC", "Asia/Kolkata"])
async def test_a_naive_utc_write_keeps_its_instant_in_any_host_zone(table: str, zone: str) -> None:
    env = {**os.environ, "TZ": zone, "PYTHONPATH": str(ROOT)}
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", ROUND_TRIP, table, WRITTEN],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    ms, s, h, read_back = done.stdout.split()[-4:]
    assert (int(ms), int(s), int(h)) == (EPOCH_MS, EPOCH_MS // 1000, EPOCH_MS // 1000)
    assert read_back == "True"
