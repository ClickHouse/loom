"""Two Loom schemas on one ClickHouse service, against a live ClickHouse: a second
schema migrated into `LOOM_CH_DATABASE` / `LOOM_CH_TELEMETRY_DATABASE` beside the
one in `loom` keeps its tables, its ledger, its KeeperMap keys and its writes to
itself, and a name with no database is refused while the first schema exists.

The CI job migrated `loom` before the tests run. Skips cleanly when LOOM_CH_HOST
is unset."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from server.core.paths import MIGRATIONS_DIR

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get("LOOM_CH_HOST"),
        reason="LOOM_CH_HOST not set — integration tests require a live CH",
    ),
]

ROOT = Path(__file__).resolve().parents[2]

# Runs in a process whose settings name the second schema: the async client's
# statements and insert targets name `loom`, and land there.
_WRITE = """
import asyncio
from server.core.ch import get_client

async def main():
    ch = get_client()
    await ch.connect()
    await ch.execute("CREATE TABLE loom._itest_names (n UInt8) ENGINE = MergeTree ORDER BY n")
    await ch.insert("loom._itest_names", [{"n": 7}])
    rows = await ch.query_all("SELECT n FROM loom._itest_names")
    print("read", [r["n"] for r in rows])

asyncio.run(main())
"""


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env={**os.environ, "LOOM_ENV_FILE": "", **env},
        capture_output=True,
        text=True,
        timeout=900,
    )


def _scalar(ch_client, sql: str, **params: str) -> object:
    rows = ch_client.query(sql, parameters=params).result_rows
    return rows[0][0] if rows else None


def test_a_second_schema_beside_loom_keeps_to_itself(ch_client) -> None:
    tag = uuid.uuid4().hex[:8]
    schema, telemetry = f"loom-itest-{tag}", f"loomitest_{tag}_telemetry"
    env = {"LOOM_CH_DATABASE": schema, "LOOM_CH_TELEMETRY_DATABASE": telemetry}
    loom_ledger = _scalar(ch_client, "SELECT count() FROM loom._migrations FINAL")
    assert loom_ledger, "the CI job did not migrate loom"
    try:
        # A name with no database is refused while a schema is in `loom`.
        refused = _run(env, "-m", "server.core.schema.migrate")
        assert refused.returncode != 0, refused.stdout + refused.stderr
        assert "LOOM_CH_DATABASE=loom" in refused.stdout + refused.stderr
        assert not _scalar(
            ch_client, "SELECT count() FROM system.databases WHERE name = {d:String}", d=schema
        )

        # Created empty, it takes a schema of its own.
        ch_client.command(f"CREATE DATABASE `{schema}`")
        done = _run(env, "-m", "server.core.schema.migrate")
        assert done.returncode == 0, done.stdout + done.stderr

        files = len(list(MIGRATIONS_DIR.glob("*.sql")))
        assert _scalar(ch_client, f"SELECT count() FROM `{schema}`._migrations FINAL") == files
        assert _scalar(ch_client, "SELECT count() FROM loom._migrations FINAL") == loom_ledger
        engine = _scalar(
            ch_client,
            "SELECT engine_full FROM system.tables WHERE database = {d:String} AND name = 'task_leases'",
            d=schema,
        )
        assert f"KeeperMap('/{schema}/task_leases')" in str(engine)
        assert _scalar(
            ch_client,
            "SELECT count() FROM system.tables WHERE database = {d:String} AND name = 'sli_minute'",
            d=telemetry,
        )

        wrote = _run(env, "-c", _WRITE)
        assert wrote.returncode == 0, wrote.stdout + wrote.stderr
        assert "read [7]" in wrote.stdout
        assert _scalar(ch_client, f"SELECT count() FROM `{schema}`._itest_names") == 1
        assert not _scalar(
            ch_client,
            "SELECT count() FROM system.tables WHERE database = 'loom' AND name = '_itest_names'",
        )
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS `{schema}` SYNC")
        ch_client.command(f"DROP DATABASE IF EXISTS `{telemetry}` SYNC")
