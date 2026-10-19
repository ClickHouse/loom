"""A write never lands beneath a delete that committed before it.

memory.set and memory.bulk_set draw a row's version (wall-clock nanoseconds) when the
write begins, then embed. A delete of the key that commits during the embed draws a
higher version, and its tombstone would hide the write that finished after it. Right
before the insert, when every committed version of a key above the rows' versions is a
delete's tombstone, the rows take new versions just above the newest one, in their own
order, and so do their embedding rows. A write whose version is above the tombstone's by
more than the spread below stays above them, and a write the key had moved past before
the delete (a later version came and went) is not brought back. A blacklist sweep's
tombstone is not a delete: a write it caught stays under it.
"""

from __future__ import annotations

import logging
import zlib
from typing import Any

from fastapi import HTTPException

from server.observability import degradations

log = logging.getLogger(__name__)

# A lifted row sits within this many nanoseconds above the tombstone (plus one per row of
# its key in the batch), offset by a hash of its own id and drawn version so two writes
# lifted over one delete rarely share a version, whatever the clock's resolution.
_LIFT_SPREAD_NS = 1_000


async def lift_above_deletes(
    org: str,
    namespace: str,
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    *,
    conditional: bool = False,
    ch: Any = None,
) -> dict[tuple[str, int], int]:
    """New versions for the rows of each key whose committed versions above them are all
    delete tombstones: just above the newest, in the rows' own order; their embedding rows
    follow. (memory_key, old version) → new, for each row lifted. A `conditional` write
    (its precondition read the row the delete removed) is refused instead (409)."""
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
        ordered = sorted(rows, key=lambda r: int(r["version"]))
        lowest = int(ordered[0]["version"])
        above = [(v, deleted) for v, deleted in committed.get(key, []) if v > lowest]
        if not above or not all(deleted for _, deleted in above):
            continue
        if conditional:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PRECONDITION_FAILED",
                    "message": "the key was deleted after the if_match_sha256 check",
                },
            )
        spread = zlib.crc32(f"{ordered[0]['memory_id']}:{lowest}".encode()) % _LIFT_SPREAD_NS
        base = above[0][0] + 1 + spread
        for i, row in enumerate(ordered):
            old, new = int(row["version"]), base + i
            row["version"] = new
            for vec in embedding_rows:
                same = (vec["agent"], vec["user_id"], vec["session_id"], vec["memory_key"]) == key
                if same and int(vec["version"]) == old:
                    vec["version"] = new
            lifted[(key[3], old)] = new
    return lifted
