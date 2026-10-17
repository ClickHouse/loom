"""The version a ReplacingMergeTree(version) row is written with."""

from __future__ import annotations

from typing import Any

from server.core.ch import own_write_reads


async def next_version(ch: Any, table: str, column: str, key: str) -> int:
    """A version past the newest row of `key` in `table`, on ClickHouse's clock.
    Under FINAL the highest version wins, not the latest write, so it must pass
    whatever any host wrote; and the clock is the one the `version` column's
    `now64(9)` default reads, which is what older builds write with."""
    with own_write_reads():  # the newest row may have landed a moment ago elsewhere
        row = await ch.query_one(
            "SELECT greatest(toUInt64(toUnixTimestamp64Nano(now64(9))), max(version) + 1) AS v "
            f"FROM {table} WHERE {column} = {{k:String}}",
            params={"k": key},
        )
    return int(row["v"])
