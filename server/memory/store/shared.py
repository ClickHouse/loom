"""Repository for loom.shared — the namespace-wide promoted-memory plane.

memory.promote inserts here (with a sentinel agent='_shared' embedding
row on the embeddings side); memory.demote tombstones via RMT(version)
plus tombstones the matching scope='shared' embedding.

Embedding-row side-effects also live here (insert at promote time,
versioned re-emit tombstone at demote time) since they're tightly coupled
to the shared lifecycle — keeping them off in a separate repo would
let the two go out of sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.core.rmt_carry import register

# Every persisted column of loom.shared, in DDL order. `ch.insert` takes its
# column list from the row, and FINAL serves the DDL default of a column the
# newest version omits over the value the promotion wrote.
# The agent of a promoted row's embedding: shared rows are not agent-scoped.
SHARED_AGENT = "_shared"

SHARED_PERSISTED_COLS: tuple[str, ...] = (
    "org",
    "namespace",
    "memory_key",
    "value",
    "memory_type",
    "confidence",
    "promoted_from",
    "promotion_event",
    "promoted_from_candidate_id",
    "promoted_by",
    "rationale",
    "promoted_at",
    "reversible_until",
    "superseded_by",
    "tombstoned_at",
    "version",
)
register("loom.shared", key=("org", "namespace", "memory_key"), columns=SHARED_PERSISTED_COLS)


async def insert_shared_row(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert one full loom.shared version row: promote (a new row) and
    demote (the live row again with `tombstoned_at` set and a higher
    version). A row missing a persisted column, or naming an unknown one,
    raises ValueError before anything is written."""
    missing = sorted(set(SHARED_PERSISTED_COLS) - set(row))
    unknown = sorted(set(row) - set(SHARED_PERSISTED_COLS))
    if missing or unknown:
        raise ValueError(
            f"loom.shared row ({row.get('memory_key', '?')!r}) is not a full version row: "
            f"missing={missing} unknown={unknown}"
        )
    ch = ch or _default_client()
    await ch.insert(
        "loom.shared",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def insert_shared_embedding(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert the scope='shared' embedding mirror at promote time.

    The embedding lives in loom.memory_embeddings with agent='_shared'
    and scope='shared' (sentinels — see memory.search's WHERE filter).
    Built through `build_embedding_row` like every other embeddings write:
    a raw dict leaves the app-side lemma column empty (the lemma lexical leg
    never finds the row) and every unnamed column at its DDL default.
    """
    from server.memory.store.rows import build_embedding_row

    ch = ch or _default_client()
    await ch.insert(
        "loom.memory_embeddings",
        [build_embedding_row(**row)],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def fetch_shared(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """Get the latest shared row for (org, ns, memory_key). FINAL
    collapses earlier promotions/demotes. Returns the row dict; the
    caller checks `tombstoned_at` for liveness.

    `promoted_from_candidate_id` is NULL for the auto-promote path;
    populated by learnings.review so memory.lineage resolves the
    candidate by FK lookup instead of a time-window match.
    """
    ch = ch or _default_client()
    return await ch.query_one(
        """
        SELECT value, memory_type, confidence, promoted_from, promotion_event,
               promoted_from_candidate_id,
               promoted_by, rationale, promoted_at, reversible_until, tombstoned_at
        FROM loom.shared FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key = {memory_key:String}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "memory_key": memory_key},
    )


async def find_candidate_for_promotion(
    org: str,
    namespace: str,
    memory_key: str,
    promoted_at: datetime,
    window_seconds: int,
    *,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """Best-effort match a learnings_queue candidate to a promotion event,
    for a shared row whose `promoted_from_candidate_id` is empty (only the
    learnings.review path sets it; auto-promote bypasses the queue). The
    candidate is then recoverable by matching:
      (org, namespace, memory_key, status='promoted',
       decided_at within +/- window of promoted_at)

    Returns the candidate row or None. The handler tags the result
    with `match_confidence='inferred'` so callers know this isn't a
    foreign-key join.
    """
    ch = ch or _default_client()
    return await ch.query_one(
        """
        SELECT candidate_id, source_kind, supporter_count, rationale,
               decided_at, decided_by, status,
               seeded_from_thread_id, seeded_from_msg_id
        FROM loom.learnings_queue FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND memory_key = {k:String}
          AND status = 'promoted'
          AND decided_at BETWEEN
              ({pa:DateTime} - INTERVAL {win:UInt32} SECOND)
              AND ({pa:DateTime} + INTERVAL {win:UInt32} SECOND)
        ORDER BY abs(toUnixTimestamp(decided_at) - toUnixTimestamp({pa:DateTime})) ASC
        LIMIT 1
        """,
        params={
            "org": org,
            "ns": namespace,
            "k": memory_key,
            "pa": promoted_at,
            "win": int(window_seconds),
        },
    )


async def tombstone_shared_embedding(
    org: str,
    namespace: str,
    memory_key: str,
    when: datetime,
    *,
    version: int,
    ch: _CHClient | None = None,
) -> None:
    """Mark the scope='shared' embedding tombstoned so memory.search
    stops returning the demoted memory. `version` is the loom.shared
    tombstone's: rows at or above it are a newer write's and are kept. A
    versioned re-emit of the live shared rows with `tombstoned_at` set, not
    an ALTER UPDATE: a mutation rewrites parts through Keeper, and any
    UPDATE on the vector table costs the fact leg its codes-first read. The
    insert is committed, so demote's contract holds: a read after demote sees
    the absence."""
    import server.memory.store as memory_repo

    ch = ch or _default_client()
    await memory_repo.reemit_shared_embedding_rows(
        org, namespace, memory_key, ch=ch, tombstoned_at=when, version=int(version)
    )
