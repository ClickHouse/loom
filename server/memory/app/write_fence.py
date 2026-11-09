"""A write never lands beneath a delete that committed before it.

memory.set and memory.bulk_set draw a row's version (wall-clock nanoseconds) when the
write begins, then embed. A delete of the key that commits during the embed draws a
higher version, and its tombstone would hide the write that finished after it. Right
before the insert, when every committed version of a key above the rows' versions is a
delete's tombstone, the rows below the newest one move just above it, and every row
keeps its order without any moving down; their embedding rows follow. A write whose
version is above the tombstone's by more than the spread below stays above them, and a
write the key had moved past before the delete (a later version came and went) is not
brought back. A blacklist sweep's tombstone is not a delete: a write it caught stays
under it.

memory.delete_version names a version, so a write after it stays whichever lands first.
A plain write takes no key lock, so a delete_version can tombstone the key after the
fence read and before the insert. Right after the insert, a row that sits under such a
tombstone (one aimed at an older version) is written again above it.

On the ClickHouse authority only: under Postgres the callers skip both lifts. The commit
locks the key's head and refuses a write whose head moved since it was read (a delete, a
delete_version), so the write starts over with a version drawn after it, and ClickHouse
holds only what has been published (storage-authority.md, "Publication"), so a read of it
could not fence a write anyway.
"""

from __future__ import annotations

import logging
import zlib
from typing import Any

from fastapi import HTTPException

from server.memory.api.models_bulk import MAX_BULK_ENTRIES
from server.observability import degradations

log = logging.getLogger(__name__)

# A lifted row sits within this many nanoseconds above the tombstone (plus one per row of
# its key in the batch), offset by a hash of its own id and drawn version so two writes
# lifted over one delete rarely share a version, whatever the clock's resolution.
LIFT_SPREAD_NS = 1_000
# How far above the tombstone a lifted row can reach: the spread, plus one per further row
# of its key in a batch.
LIFT_BAND_NS = LIFT_SPREAD_NS + MAX_BULK_ENTRIES

Key = tuple[str, str, str, str]


def _key(row: dict[str, Any]) -> Key:
    return (row["agent"], row["user_id"], row["session_id"], row["memory_key"])


async def _committed_above(
    org: str, namespace: str, memory_rows: list[dict[str, Any]], ch: Any
) -> dict[Key, list[tuple[int, bool, int]]] | None:
    """`versions_above` for the rows, or None (and a noted degradation) when it fails: the
    fence must not fail the write it guards."""
    import server.memory.store as memory_repo

    try:
        return await memory_repo.versions_above(org, namespace, memory_rows, ch=ch)
    except Exception as e:  # noqa: BLE001 — the fence must not fail the write it guards
        degradations.note("write_fence", "versions_kept", e)
        log.warning("write fence: committed versions unread, rows keep their versions: %s", e)
        return None


def _lift(
    rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    tombstone: int,
    lifted: dict[tuple[str, int], int],
) -> None:
    """Move the rows of one key under `tombstone` just above it, in their own order, without
    moving any row down; their embedding rows follow."""
    ordered = sorted(rows, key=lambda r: int(r["version"]))
    lowest = int(ordered[0]["version"])
    spread = zlib.crc32(f"{ordered[0]['memory_id']}:{lowest}".encode()) % LIFT_SPREAD_NS
    floor = tombstone + 1 + spread
    previous = 0
    for row in ordered:
        old = int(row["version"])
        new = max(old if old > tombstone else floor, previous + 1)
        previous = new
        if new == old:
            continue
        row["version"] = new
        for vec in embedding_rows:
            if _key(vec) == _key(row) and int(vec["version"]) == old:
                vec["version"] = new
        lifted[(row["memory_key"], old)] = new


async def lift_above_deletes(
    org: str,
    namespace: str,
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    *,
    conditional: bool = False,
    ch: Any = None,
) -> dict[tuple[str, int], int]:
    """Before the insert: new versions for the rows of each key whose committed versions
    above them are all delete tombstones, just above the newest, in the rows' own order;
    their embedding rows follow. (memory_key, old version) → new, for each row moved. A
    `conditional` write (its precondition read the row the delete removed) is refused
    instead (409)."""
    committed = await _committed_above(org, namespace, memory_rows, ch)
    lifted: dict[tuple[str, int], int] = {}
    if not committed:
        return lifted
    by_key: dict[Key, list[dict[str, Any]]] = {}
    for row in memory_rows:
        by_key.setdefault(_key(row), []).append(row)
    for key, rows in by_key.items():
        lowest = min(int(r["version"]) for r in rows)
        above = [(v, deleted) for v, deleted, _t in committed.get(key, []) if v > lowest]
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
        _lift(rows, embedding_rows, above[0][0], lifted)
    return lifted


async def lift_after_insert(
    org: str,
    namespace: str,
    memory_rows: list[dict[str, Any]],
    embedding_rows: list[dict[str, Any]],
    *,
    ch: Any = None,
) -> dict[tuple[str, int], int]:
    """After the insert: the rows of each key that sit under memory.delete_version
    tombstones only, each aimed at a version older than the rows, are written again just
    above the newest (their embedding rows too), and the rows given carry the new versions.
    (memory_key, old version) → new, for each row written again."""
    import server.memory.store as memory_repo

    committed = await _committed_above(org, namespace, memory_rows, ch)
    lifted: dict[tuple[str, int], int] = {}
    if not committed:
        return lifted
    by_key: dict[Key, list[dict[str, Any]]] = {}
    for row in memory_rows:
        by_key.setdefault(_key(row), []).append(row)
    for key, rows in by_key.items():
        lowest = min(int(r["version"]) for r in rows)
        # The batch's own rows are in by now: the key's other versions decide.
        own = {int(r["version"]) for r in rows}
        above = [(v, d, t) for v, d, t in committed.get(key, []) if v > lowest and v not in own]
        if above and all(d and 0 < t < lowest for _v, d, t in above):
            _lift(rows, embedding_rows, above[0][0], lifted)
    if not lifted:
        return lifted
    moved = set(lifted.values())
    await memory_repo.insert_memory_versions(
        [r for r in memory_rows if r["version"] in moved], ch=ch
    )
    # Each copy through the builder, as every embedding write is: the rows were built by it.
    lifted_vectors = [
        memory_repo.build_embedding_row(**v) for v in embedding_rows if v["version"] in moved
    ]
    if lifted_vectors:
        await memory_repo.insert_embedding_rows(lifted_vectors, ch=ch)
    return lifted
