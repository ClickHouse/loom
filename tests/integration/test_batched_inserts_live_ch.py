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

        # Read back at once: the insert returned after its part was written.
        await c.insert(table, [{"k": "one", "v": "a"}], settings=batched_insert_settings())
        row = await c.query_one(f"SELECT count() AS n FROM {table} WHERE k = 'one'")
        assert row is not None and row["n"] == 1

        # Concurrent inserts share parts: fewer parts than inserts.
        await asyncio.gather(
            *(
                c.insert(table, [{"k": f"c{i}", "v": "b"}], settings=batched_insert_settings())
                for i in range(40)
            )
        )
        rows = await c.query_one(f"SELECT count() AS n FROM {table} WHERE startsWith(k, 'c')")
        assert rows is not None and rows["n"] == 40
        parts = await c.query_one(
            f"SELECT uniqExact(_part) AS n FROM {table} WHERE startsWith(k, 'c')"
        )
        assert parts is not None and 1 <= parts["n"] < 40

        # A batch sent twice with one token (a retry) lands once, in sequence and at once.
        token = {**batched_insert_settings(), "insert_deduplication_token": uuid.uuid4().hex}
        await c.insert(table, [{"k": "retry", "v": "x"}], settings=dict(token))
        await c.insert(table, [{"k": "retry", "v": "x"}], settings=dict(token))
        racing = {**batched_insert_settings(), "insert_deduplication_token": uuid.uuid4().hex}
        await asyncio.gather(
            *(c.insert(table, [{"k": "race", "v": "y"}], settings=dict(racing)) for _ in range(2))
        )
        got = await c.query_all(
            f"SELECT k, count() AS n FROM {table} WHERE k IN ('retry', 'race') GROUP BY k"
        )
        assert {r["k"]: r["n"] for r in got} == {"retry": 1, "race": 1}
    finally:
        await c.execute(f"DROP TABLE IF EXISTS {table}")
        await c.disconnect()
