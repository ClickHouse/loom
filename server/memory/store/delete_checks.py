"""loom.delete_version_checks: the check memory.delete_version owes after its tombstone.

One row per delete, written before the tombstone (`done = 0`) and again once the check
has run (`done = 1`). A row still at `done = 0` is a check that did not finish; the
delete_version_check worker runs it.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.memory.store.sql import _SEQUENTIAL, read_your_writes_enabled

TABLE = "loom.delete_version_checks"


# A check's identity, key and versions: what `record_delete_check` writes besides `done`.
_CHECK_COLUMNS = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "checked",
    "tombstone",
)


async def record_delete_check(
    check: dict[str, Any], *, done: bool, ch: _CHClient | None = None
) -> None:
    """The row of `check` (the delete of `memory_key` at version `checked`, tombstoned at
    `tombstone`, under its identity), committed before the call returns."""
    ch = ch or _default_client()
    row = {k: check[k] for k in _CHECK_COLUMNS}
    row["checked"], row["tombstone"] = int(row["checked"]), int(row["tombstone"])
    row["done"] = 1 if done else 0
    await ch.insert(TABLE, [row], settings={"async_insert": 0, "wait_for_async_insert": 1})


async def find_delete_check(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    checked: int,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """The newest check of a delete of this key at version `checked` under this identity,
    done or not, or None when no delete recorded one."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, checked, tombstone, done
        FROM {TABLE} FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}} AND agent = {{agent:String}}
          AND user_id = {{uid:String}} AND session_id = {{sid:String}}
          AND memory_key = {{key:String}} AND checked = {{checked:UInt64}}
        ORDER BY tombstone DESC
        LIMIT 1
        {_SEQUENTIAL if read_your_writes_enabled() else ""}
        """,
        params={
            "org": org,
            "ns": namespace,
            "agent": agent,
            "uid": user_id,
            "sid": session_id,
            "key": memory_key,
            "checked": int(checked),
        },
    )
    return rows[0] if rows else None


async def pending_delete_checks(
    *, older_than_seconds: int, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Checks still owed, oldest first, written at least `older_than_seconds` ago (so a
    delete still running its own check is left to it); at most `limit`."""
    ch = ch or _default_client()
    return await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, checked, tombstone
        FROM {TABLE} FINAL
        WHERE done = 0 AND created_at <= now() - toIntervalSecond({{age:UInt32}})
        ORDER BY created_at
        LIMIT {{limit:UInt32}}
        {_SEQUENTIAL if read_your_writes_enabled() else ""}
        """,
        params={"age": int(older_than_seconds), "limit": int(limit)},
    )
