"""A write never lands beneath a delete that committed before it.

memory.set and memory.bulk_set draw a row's version (wall-clock nanoseconds) when the
write begins, then embed. A delete of the key that commits during the embed draws a
higher version, and its tombstone would hide the write that finished after it. Right
before the insert, when the newest committed version of a key is a delete's tombstone
above the rows' versions, the rows take new versions above it, in their own order, and
so do their embedding rows. A write drawn after the delete keeps winning over them, as
it would without the delete. A blacklist sweep's tombstone is not a delete: a write it
caught stays under it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from server.memory.store.rows import next_version
from server.observability import degradations

log = logging.getLogger(__name__)


async def lift_above_deletes(
    org: str,
    namespace: str,
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    *,
    conditional: bool = False,
    ch: Any = None,
) -> dict[tuple[str, int], int]:
    """New versions for the rows of each key whose newest committed version is a delete's
    tombstone above them, in the rows' own order; their embedding rows follow.
    (memory_key, old version) → new, for each row lifted. A `conditional` write (its
    precondition read the row the delete removed) is refused instead (409)."""
    import server.memory.store as memory_repo

    try:
        committed = await memory_repo.versions_above(org, namespace, memory_rows, ch=ch)
    except Exception as e:  # noqa: BLE001 — the fence must not fail the write it guards
        degradations.note("write_fence", "versions_kept", e)
        log.warning("write fence: committed versions unread, rows keep their versions: %s", e)
        return {}
    lifted: dict[tuple[str, int], int] = {}
    if not committed:
        return lifted
    by_key: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in memory_rows:
        key = (row["agent"], row["user_id"], row["session_id"], row["memory_key"])
        by_key.setdefault(key, []).append(row)
    for key, rows in by_key.items():
        lowest = min(int(r["version"]) for r in rows)
        above = [(v, deleted) for v, deleted in committed.get(key, []) if v > lowest]
        if not above or not above[0][1]:
            continue
        if conditional:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PRECONDITION_FAILED",
                    "message": "the key was deleted after the if_match_sha256 check",
                },
            )
        base = max(next_version(), above[0][0] + 1)
        for i, row in enumerate(sorted(rows, key=lambda r: int(r["version"]))):
            old, new = int(row["version"]), base + i
            row["version"] = new
            for vec in embedding_rows:
                same = (vec["agent"], vec["user_id"], vec["session_id"], vec["memory_key"]) == key
                if same and int(vec["version"]) == old:
                    vec["version"] = new
            lifted[(key[3], old)] = new
    return lifted
