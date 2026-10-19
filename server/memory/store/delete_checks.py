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


async def record_delete_check(
    existing: Any, *, checked: int, tombstone: int, done: bool, ch: _CHClient | None = None
) -> None:
    """The check row of the delete of `existing` (its identity and key) at `checked`,
    tombstoned at `tombstone`; committed before the call returns."""
    ch = ch or _default_client()
    await ch.insert(
        TABLE,
        [
            {
                "org": existing.org,
                "namespace": existing.namespace,
                "agent": existing.agent,
                "user_id": existing.user_id,
                "session_id": existing.session_id,
                "memory_key": existing.memory_key,
                "checked": int(checked),
                "tombstone": int(tombstone),
                "done": 1 if done else 0,
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


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
