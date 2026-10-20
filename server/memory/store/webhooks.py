"""loom.memory_webhooks, loom.memory_webhook_outbox and loom.memory_webhook_attempts:
every statement of memory webhooks.

A registration is a ReplacingMergeTree row rewritten whole (`WEBHOOK_COLS`). An
outbox row is written once by the write path and never again (`OUTBOX_COLS`).
The delivery worker only appends attempt rows (`ATTEMPT_COLS`): event ids,
counts and HTTP statuses, the newest `version` of an event winning. So an
erasure that deletes an event's outbox row leaves nothing a later pass can
send, whatever a pass in flight writes after it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.ch import own_write_reads
from server.core.repo_base import _CHClient, _default_client

WEBHOOKS = "loom.memory_webhooks"
OUTBOX = "loom.memory_webhook_outbox"
ATTEMPTS = "loom.memory_webhook_attempts"

# Days an outbox row and an attempt row are kept (the tables' TTL).
KEEP_DAYS = 3

WEBHOOK_COLS: tuple[str, ...] = (
    "org",
    "namespace",
    "webhook_id",
    "url",
    "secret_ref",
    "events",
    "created_by",
    "created_at",
    "is_deleted",
    "version",
)

OUTBOX_COLS: tuple[str, ...] = (
    "org",
    "namespace",
    "webhook_id",
    "event_id",
    "event_type",
    "agent",
    "user_id",
    "session_id",
    "memory_key",
    "memory_id",
    "memory_version",
    "actor",
    "request_id",
    "erasure_id",
    "occurred_at",
)

ATTEMPT_COLS: tuple[str, ...] = (
    "event_id",
    "status",
    "attempts",
    "next_attempt_at",
    "last_status",
    "last_error",
    "recorded_at",
    "version",
)

# The identity tuple and key an outbox row names a memory by.
KEY_COLS = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")

_COMMITTED = {"async_insert": 0, "wait_for_async_insert": 1}

# Each event's newest attempt row, of the attempts recorded since {since}.
_LATEST_ATTEMPT = f"""
    SELECT event_id,
           argMax(status, version) AS status,
           argMax(attempts, version) AS attempts,
           argMax(next_attempt_at, version) AS next_attempt_at,
           argMax(last_status, version) AS last_status,
           argMax(last_error, version) AS last_error
    FROM {ATTEMPTS}
    WHERE recorded_at >= {{since:DateTime64(3)}}
    GROUP BY event_id
"""


def _whole(row: dict[str, Any], cols: tuple[str, ...]) -> dict[str, Any]:
    """`row` with exactly `cols`: a missing column raises rather than landing
    its DEFAULT."""
    return {c: row[c] for c in cols}


async def insert_webhook(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(WEBHOOKS, [_whole(row, WEBHOOK_COLS)], settings=_COMMITTED)


async def namespace_webhooks(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The namespace's live registrations, oldest first."""
    ch = ch or _default_client()
    with own_write_reads():  # a list right after a register shows it
        return await ch.query_all(
            f"SELECT {', '.join(WEBHOOK_COLS)} FROM {WEBHOOKS} FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} AND is_deleted = 0 "
            "ORDER BY created_at, webhook_id LIMIT 1000",
            params={"org": org, "ns": namespace},
        )


async def live_webhooks(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Every live registration of the deployment: what each process's copy and
    the delivery worker read. Registrations are few (a cap per namespace)."""
    ch = ch or _default_client()
    with own_write_reads():
        return await ch.query_all(
            f"SELECT {', '.join(WEBHOOK_COLS)} FROM {WEBHOOKS} FINAL WHERE is_deleted = 0"
        )


async def insert_outbox(rows: list[dict[str, Any]], *, ch: _CHClient | None = None) -> None:
    """Outbox rows, committed before the call returns."""
    if not rows:
        return
    ch = ch or _default_client()
    await ch.insert(OUTBOX, [_whole(r, OUTBOX_COLS) for r in rows], settings=_COMMITTED)


async def insert_attempts(rows: list[dict[str, Any]], *, ch: _CHClient | None = None) -> None:
    if not rows:
        return
    ch = ch or _default_client()
    await ch.insert(ATTEMPTS, [_whole(r, ATTEMPT_COLS) for r in rows], settings=_COMMITTED)


async def due_events(
    now: datetime,
    *,
    since: datetime,
    limit: int,
    org: str = "",
    namespace: str = "",
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Outbox rows since `since` with no attempt yet, or whose newest attempt is
    pending (or a lease) and due, oldest first, at most `limit`; each with its
    `attempts` so far. `org` and `namespace` narrow it when set (tests)."""
    ch = ch or _default_client()
    where = ["o.occurred_at >= {since:DateTime64(3)}"]
    params: dict[str, Any] = {"now": now, "since": since, "limit": int(limit)}
    if org:
        where.append("o.org = {org:String}")
        params["org"] = org
    if namespace:
        where.append("o.namespace = {ns:String}")
        params["ns"] = namespace
    cols = ", ".join(f"o.{c} AS {c}" for c in OUTBOX_COLS)
    with own_write_reads():  # a lease just written must be seen
        return await ch.query_all(
            f"SELECT {cols}, ifNull(a.attempts, 0) AS attempts "
            f"FROM {OUTBOX} AS o LEFT JOIN ({_LATEST_ATTEMPT}) AS a ON a.event_id = o.event_id "
            f"WHERE {' AND '.join(where)} AND (ifNull(a.event_id, '') = '' "
            "OR (a.status IN ('pending', 'leased') AND a.next_attempt_at <= {now:DateTime64(3)})) "
            "ORDER BY o.occurred_at LIMIT {limit:UInt32}",
            params=params,
        )


async def keys_with_history(
    keys: list[tuple[str, ...]], *, ch: _CHClient | None = None
) -> set[tuple[str, ...]]:
    """Which of `keys` (KEY_COLS tuples) still have a row in loom.memory_versions.
    An erasure deletes a key's history (a delete or a redaction keeps it), so a
    key without one was erased."""
    if not keys:
        return set()
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT DISTINCT {', '.join(KEY_COLS)} FROM loom.memory_versions "
        f"WHERE ({', '.join(KEY_COLS)}) IN "
        "{keys:Array(Tuple(String, String, String, String, String, String))}",
        params={"keys": [tuple(k) for k in keys]},
    )
    return {tuple(str(r[c]) for c in KEY_COLS) for r in rows}


async def namespace_deliveries(
    org: str,
    namespace: str,
    *,
    since: datetime,
    webhook_id: str,
    status: str,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The namespace's events since `since` with their newest attempt, newest
    first; a lease reads as `pending`. `webhook_id` and `status` narrow it when
    set."""
    ch = ch or _default_client()
    where = ["o.org = {org:String}", "o.namespace = {ns:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace, "since": since, "limit": int(limit)}
    if webhook_id:
        where.append("o.webhook_id = {wid:String}")
        params["wid"] = webhook_id
    outer = "WHERE status = {status:String}" if status else ""
    if status:
        params["status"] = status
    return await ch.query_all(
        f"""
        SELECT * FROM (
            SELECT o.event_id AS event_id, o.webhook_id AS webhook_id,
                   o.event_type AS event_type, o.memory_key AS memory_key,
                   o.memory_id AS memory_id, o.memory_version AS memory_version,
                   o.occurred_at AS occurred_at,
                   if(ifNull(a.event_id, '') = '' OR a.status = 'leased', 'pending', a.status)
                       AS status,
                   ifNull(a.attempts, 0) AS attempts,
                   if(ifNull(a.event_id, '') = '', o.occurred_at, a.next_attempt_at)
                       AS next_attempt_at,
                   ifNull(a.last_status, 0) AS last_status,
                   ifNull(a.last_error, '') AS last_error
            FROM {OUTBOX} AS o
            LEFT JOIN ({_LATEST_ATTEMPT}) AS a ON a.event_id = o.event_id
            WHERE o.occurred_at >= {{since:DateTime64(3)}} AND {" AND ".join(where)}
        )
        {outer}
        ORDER BY occurred_at DESC
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )
