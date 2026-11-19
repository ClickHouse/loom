"""The version a ReplacingMergeTree(version) row is written with.

`next_version` serves the tables several replicas write for one key at once
(revoked_tokens, sso_links, scim_users, machine_identities, and a new
service account under a name another host revoked). A refresh grant's
loom.oauth_state row and a revoked service account stay on the host's clock:
each is rewritten under a claim on its key, past the version of the row it
read there, so that is enough to replace it."""

from __future__ import annotations

from typing import Any

from server.core.ch import own_write_reads


async def next_version(
    ch: Any,
    table: str,
    key: dict[str, str],
    *,
    deadline: float | None = None,
) -> int:
    """A version past the newest row of `key` (its columns' values) in `table`,
    on ClickHouse's clock. Under FINAL the highest version wins, not the latest
    write, so it must pass whatever any host wrote; one clock for every host
    keeps their writes in order."""
    where = " AND ".join(f"{column} = {{k{i}:String}}" for i, column in enumerate(key))
    if deadline is not None:
        from server.core.ch_bounded import BoundedReadError, read_rows

        rows = await read_rows(
            "SELECT toUInt64(toUnixTimestamp64Nano(now64(9))) AS clock, max(version) AS previous "
            f"FROM {table} WHERE {where} LIMIT 2 FORMAT JSONEachRow",
            {f"k{i}": value for i, value in enumerate(key.values())},
            ch=ch,
            deadline=deadline,
            row_bytes=1024,
            total_bytes=1024,
            max_rows=1,
            tag="cell.each.row_version",
        )
        if (
            len(rows) != 1
            or set(rows[0]) != {"clock", "previous"}
            or any(
                type(rows[0][name]) is not int or not 0 <= rows[0][name] < 2**64 - 1
                for name in ("clock", "previous")
            )
        ):
            raise BoundedReadError("A successor version could not be established")
        clock: int = rows[0]["clock"]
        previous: int = rows[0]["previous"]
        return max(clock, previous + 1)
    with own_write_reads():  # the newest row may have landed a moment ago elsewhere
        row = await ch.query_one(
            "SELECT greatest(toUInt64(toUnixTimestamp64Nano(now64(9))), max(version) + 1) AS v "
            f"FROM {table} WHERE {where}",
            params={f"k{i}": value for i, value in enumerate(key.values())},
        )
    return int(row["v"])
