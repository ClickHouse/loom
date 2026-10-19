"""A write never lands beneath a delete that committed before it.

memory.set and memory.bulk_set draw a row's version (wall-clock nanoseconds) when the
write begins, then embed. A delete of the key that commits during the embed draws a
higher version, and its tombstone would hide the write that finished after it. Right
before the insert, each row whose key has a tombstone above its version takes a new
version, and so do its embedding rows.
"""

from __future__ import annotations

from typing import Any

from server.memory.store.rows import next_version


async def lift_above_tombstones(
    org: str,
    namespace: str,
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    *,
    ch: Any = None,
) -> dict[int, int]:
    """Give each memory row a version above any tombstone of its key that committed since
    the row's version was drawn, and its embedding rows the same. Old version → new, for
    each row lifted."""
    import server.memory.store as memory_repo

    above = await memory_repo.tombstones_above(org, namespace, memory_rows, ch=ch)
    lifted: dict[int, int] = {}
    if not above:
        return lifted
    for row in memory_rows:
        key = (row["agent"], row["user_id"], row["session_id"], row["memory_key"])
        tombstone = above.get(key, 0)
        old = int(row["version"])
        if tombstone <= old:
            continue
        new = max(next_version(), tombstone + 1)
        row["version"] = new
        for e in embedding_rows:
            same_key = (e["agent"], e["user_id"], e["session_id"], e["memory_key"]) == key
            if same_key and int(e["version"]) == old:
                e["version"] = new
        lifted[old] = new
    return lifted
