"""The version a ReplacingMergeTree(version) row is written with.

`next_version` serves the tables several replicas write for one key at once
(revoked_tokens, sso_links, scim_users). loom.service_accounts and a refresh
grant's loom.oauth_state row stay on the host's clock: each is rewritten only
under a claim on its key, past the version of the row it read there, so that
is enough to replace it."""

from __future__ import annotations

from typing import Any

from server.core.ch import own_write_reads


async def next_version(ch: Any, table: str, column: str, key: str) -> int:
    """A version past the newest row of `key` in `table`, on ClickHouse's clock.
    Under FINAL the highest version wins, not the latest write, so it must pass
    whatever any host wrote; one clock for every host keeps their writes in order."""
    with own_write_reads():  # the newest row may have landed a moment ago elsewhere
        row = await ch.query_one(
            "SELECT greatest(toUInt64(toUnixTimestamp64Nano(now64(9))), max(version) + 1) AS v "
            f"FROM {table} WHERE {column} = {{k:String}}",
            params={"k": key},
        )
    return int(row["v"])
