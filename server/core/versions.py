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


async def next_version(ch: Any, table: str, key: dict[str, str]) -> int:
    """A version past the newest row of `key` (its columns' values) in `table`,
    on ClickHouse's clock. Under FINAL the highest version wins, not the latest
    write, so it must pass whatever any host wrote; one clock for every host
    keeps their writes in order."""
    where = " AND ".join(f"{column} = {{k{i}:String}}" for i, column in enumerate(key))
    with own_write_reads():  # the newest row may have landed a moment ago elsewhere
        row = await ch.query_one(
            "SELECT greatest(toUInt64(toUnixTimestamp64Nano(now64(9))), max(version) + 1) AS v "
            f"FROM {table} WHERE {where}",
            params={f"k{i}": value for i, value in enumerate(key.values())},
        )
    return int(row["v"])
