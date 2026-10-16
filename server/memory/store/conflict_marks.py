"""The deferred conflict judge's mark on loom.memory_embeddings.

`conflict_checked_at` is the epoch (`PENDING_MARK`) while a write's deferred
judge is owed, the settle time once it lands, and NULL on rows that never
deferred one. The mark lives on the row so every replica reads the same
`pending`, and a judge lost with its process is found again by the leader's
conflict sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server.core.datetimes import naive_utc_now, to_naive_utc
from server.core.repo_base import _CHClient, _default_client
from server.memory.store.embeddings import (
    EMB_KEY_PREWHERE,
    live_embedding_rows_for_key,
    reemit_rows,
)

PENDING_MARK = datetime(1970, 1, 1, tzinfo=UTC)  # aware: a naive one encodes as host time
# A mark older than this is a judge lost for good: read as settled, never swept.
LOOKBACK_SECONDS = 3600
# How long a mark may stand before the sweep takes the judge as lost.
GRACE_SECONDS = 60
# How long after a write memory.get reads the mark: another replica's judge, then
# the sweep's re-run of a lost one. Past it a still-marked row reads as settled.
PENDING_READ_SECONDS = 2 * GRACE_SECONDS

# Epoch seconds, so the test does not depend on the server's time zone.
_MARKED = "toUnixTimestamp(conflict_checked_at) = 0"


def is_mark(value: Any) -> bool:
    """Whether a read `conflict_checked_at` is the pending mark."""
    return isinstance(value, datetime) and value.year < 2000


async def key_has_pending_mark(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Whether the live embedding row of this identity key still owes its judge."""
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT count() AS n FROM loom.memory_embeddings FINAL
        PREWHERE {EMB_KEY_PREWHERE}
        WHERE tombstoned_at IS NULL AND {_MARKED}
        """,
        {
            "org": org,
            "ns": namespace,
            "a": agent,
            "u": user_id,
            "s": session_id,
            "k": memory_key,
        },
    )
    return bool(int((row or {}).get("n") or 0))


async def stamp_checked(
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    memory_key: str,
    *,
    version: int,
    created_at: datetime,
    content_sha256: str = "",
    ch: _CHClient | None = None,
) -> int:
    """Stamp the embedding rows of the judged write with now: those still marked at
    its `version` or above (a touch, verify or apply re-emits at read + 1) that carry
    its `content_sha256` (rows from before 038: its `created_at`). A newer write's
    row keeps its own mark for its own settle."""
    ch = ch or _default_client()
    rows = await live_embedding_rows_for_key(
        ch,
        org=org,
        namespace=namespace,
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        memory_key=memory_key,
    )
    own = [r for r in rows if _same_write(r, version, created_at, content_sha256)]
    return await reemit_rows(own, {"conflict_checked_at": naive_utc_now()}, ch=ch)


def _same_write(row: dict[str, Any], version: int, created_at: datetime, sha: str) -> bool:
    if not is_mark(row.get("conflict_checked_at")) or int(row.get("version") or 0) < int(version):
        return False
    row_sha = str(row.get("content_sha256") or "")
    if sha and row_sha:
        # Content-equal revisions share one verdict (`conflict_deferred.still_current`).
        return row_sha == sha
    # Whole seconds: two revisions within one second are told apart only by the hash.
    written = row.get("created_at")
    return isinstance(written, datetime) and _seconds(written) == _seconds(created_at)


def _seconds(dt: datetime) -> datetime:
    # created_at is a DateTime column: whole seconds.
    return to_naive_utc(dt).replace(microsecond=0)


async def stale_pending_marks(
    *, grace_s: int, lookback_s: int, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Live, active, embedded rows whose mark is older than `grace_s` (their judge
    was lost) and younger than `lookback_s`, oldest first, one per identity key."""
    ch = ch or _default_client()
    return await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, memory_type,
               embedding, created_at, version, content_sha256
        FROM loom.memory_embeddings FINAL
        WHERE created_at > now() - INTERVAL {{lookback:UInt32}} SECOND
          AND created_at < now() - INTERVAL {{grace:UInt32}} SECOND
          AND {_MARKED}
          AND tombstoned_at IS NULL
          AND scope = 'private'
          AND status = 'active'
          AND embedding_dim > 0
        ORDER BY created_at ASC
        LIMIT 1 BY org, namespace, agent, user_id, session_id, memory_key
        LIMIT {{lim:UInt32}}
        """,
        {"lookback": int(lookback_s), "grace": int(grace_s), "lim": int(limit)},
    )


__all__ = [
    "PENDING_MARK",
    "is_mark",
    "key_has_pending_mark",
    "stale_pending_marks",
    "stamp_checked",
]
