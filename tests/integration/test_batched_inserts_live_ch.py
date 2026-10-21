"""A batched insert (server/core/ch_writes.py, `batched_insert_settings`) is read back
as soon as it returns, concurrent ones share a part, and a batch sent twice with one
deduplication token lands once. Runs on a private table with the deduplication
window Loom's tables carry."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from server.core.ch import CHClient
from server.core.ch_writes import batched_insert_settings

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.asyncio
async def test_batched_inserts_are_read_back_share_parts_and_land_once_per_token() -> None:
    table = f"loom.batched_insert_{uuid.uuid4().hex[:12]}"
    c = CHClient()
    await c.connect()
    try:
        await c.execute(
            f"CREATE TABLE {table} (k String, v String) ENGINE = MergeTree ORDER BY k "
            "SETTINGS non_replicated_deduplication_window = 1000"
        )
        # No merge may fold the parts the shared-parts check counts.
        await c.execute(f"SYSTEM STOP MERGES {table}")

        # Read back at once: the insert returned after its part was written.
        await c.insert(table, [{"k": "one", "v": "a"}], settings=batched_insert_settings())
        got_one = await c.query_all(f"SELECT k, v FROM {table} WHERE k = 'one'")
        assert got_one == [{"k": "one", "v": "a"}]

        # Concurrent inserts share parts: fewer parts than inserts.
        await asyncio.gather(
            *(
                c.insert(table, [{"k": f"c{i}", "v": "b"}], settings=batched_insert_settings())
                for i in range(40)
            )
        )
        count = await c.query_one(f"SELECT count() AS n FROM {table} WHERE startsWith(k, 'c')")
        assert count is not None and count["n"] == 40
        parts = await c.query_one(
            f"SELECT uniqExact(_part) AS n FROM {table} WHERE startsWith(k, 'c')"
        )
        assert parts is not None and 1 <= parts["n"] < 40

        # A batch sent twice with one token (a retry) lands once, in sequence and at once.
        retry_settings = {
            **batched_insert_settings(),
            "insert_deduplication_token": uuid.uuid4().hex,
        }
        await c.insert(table, [{"k": "retry", "v": "x"}], settings=dict(retry_settings))
        await c.insert(table, [{"k": "retry", "v": "x"}], settings=dict(retry_settings))
        race_settings = {
            **batched_insert_settings(),
            "insert_deduplication_token": uuid.uuid4().hex,
        }
        await asyncio.gather(
            *(
                c.insert(table, [{"k": "race", "v": "y"}], settings=dict(race_settings))
                for _ in range(2)
            )
        )
        got = await c.query_all(
            f"SELECT k, count() AS n FROM {table} WHERE k IN ('retry', 'race') GROUP BY k"
        )
        assert {r["k"]: r["n"] for r in got} == {"retry": 1, "race": 1}
    finally:
        await c.execute(f"DROP TABLE IF EXISTS {table}")
        await c.disconnect()
