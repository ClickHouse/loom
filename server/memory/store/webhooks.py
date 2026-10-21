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
KEEP_DAYS = 3  # not a setting: the TTL migration 064 gives the tables
# The most registrations a registry read returns: far above any deployment's count
# (memory_webhook_max_per_namespace each), a bound on a read nothing else bounds.
REGISTRY_LIMIT = 100_000  # not a setting: a safety bound, not a page size

WEBHOOK_COLS: tuple[str, ...] = (
    "org",
    "namespace",
    "webhook_id",
    "url",
    "secret_ref",
    "events",
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

# A webhook as statements name it: (org, namespace, webhook_id), the outbox's
# sort-key prefix.
Hook = tuple[str, str, str]

_COMMITTED = {"async_insert": 0, "wait_for_async_insert": 1}
_HOOKS = "{hooks:Array(Tuple(String, String, String))}"
# An event announced twice has two outbox rows of its webhook: keep the earliest.
_EARLIEST = "ORDER BY org, namespace, webhook_id, occurred_at LIMIT 1 BY event_id"


def _latest_attempts(events: str) -> str:
    """Each event's newest attempt row, for the events the `events` subquery
    names (attempts recorded since {since})."""
    return f"""
        SELECT event_id,
               argMax(status, version) AS status,
               argMax(attempts, version) AS attempts,
               argMax(next_attempt_at, version) AS next_attempt_at,
               argMax(last_status, version) AS last_status,
               argMax(last_error, version) AS last_error
        FROM {ATTEMPTS}
        WHERE recorded_at >= {{since:DateTime64(3)}} AND event_id IN ({events})
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
            "ORDER BY created_at, webhook_id LIMIT {limit:UInt32}",
            params={"org": org, "ns": namespace, "limit": REGISTRY_LIMIT},
        )


async def live_webhooks(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    """Every live registration of the deployment: what each process's copy and
    the delivery worker read. Registrations are few (a cap per namespace)."""
    ch = ch or _default_client()
    with own_write_reads():
        return await ch.query_all(
            f"SELECT {', '.join(WEBHOOK_COLS)} FROM {WEBHOOKS} FINAL WHERE is_deleted = 0 "
            "LIMIT {limit:UInt32}",
            params={"limit": REGISTRY_LIMIT},
        )


async def removed_webhooks(since: datetime, *, ch: _CHClient | None = None) -> list[Hook]:
    """The registrations removed since `since`."""
    ch = ch or _default_client()
    with own_write_reads():
        rows = await ch.query_all(
            f"SELECT org, namespace, webhook_id FROM {WEBHOOKS} FINAL WHERE is_deleted = 1 "
            "AND version >= toUnixTimestamp64Nano({since:DateTime64(3)}) LIMIT {limit:UInt32}",
            params={"since": since, "limit": REGISTRY_LIMIT},
        )
    return [(str(r["org"]), str(r["namespace"]), str(r["webhook_id"])) for r in rows]


async def webhook_removed_since(
    hook: Hook, since: datetime, *, ch: _CHClient | None = None
) -> bool:
    """Whether `hook` was removed since `since`."""
    ch = ch or _default_client()
    with own_write_reads():
        rows = await ch.query_all(
            f"SELECT 1 FROM {WEBHOOKS} FINAL "
            "WHERE (org, namespace, webhook_id) = {hook:Tuple(String, String, String)} "
            "AND is_deleted = 1 AND version >= toUnixTimestamp64Nano({since:DateTime64(3)}) "
            "LIMIT 1",
            params={"hook": hook, "since": since},
        )
    return bool(rows)


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
    hooks: list[Hook],
    per_webhook: int,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Events of `hooks` since `since` with no attempt yet, or whose newest
    attempt is pending (or a lease) and due, each with its `attempts`, `last_status`
    and `last_error` so far.
    Round robin: every webhook's oldest due event first, then every webhook's
    second, and so on, at most `per_webhook` of each and `limit` in all.

    Two statements: the events not settled or waiting (a set of ids, not a join
    of every attempt), then the newest attempt state of the few chosen."""
    if not hooks:
        return []
    ch = ch or _default_client()
    cols = ", ".join(OUTBOX_COLS)
    with own_write_reads():  # a lease just written must be seen
        rows = await ch.query_all(
            f"SELECT {cols} FROM ("
            f"SELECT {cols}, row_number() OVER "
            "(PARTITION BY org, namespace, webhook_id ORDER BY occurred_at, event_id) AS turn "
            f"FROM (SELECT {cols} FROM {OUTBOX} WHERE (org, namespace, webhook_id) IN {_HOOKS} "
            "AND occurred_at >= {since:DateTime64(3)} "
            f"{_EARLIEST}) "
            f"WHERE event_id NOT IN (SELECT event_id FROM {ATTEMPTS} "
            "WHERE recorded_at >= {since:DateTime64(3)} GROUP BY event_id "
            "HAVING argMax(status, version) IN ('delivered', 'dead') "
            "OR argMax(next_attempt_at, version) > {now:DateTime64(3)})"
            ") WHERE turn <= {per:UInt32} "
            "ORDER BY turn, occurred_at, event_id LIMIT {limit:UInt32}",
            params={
                "now": now,
                "since": since,
                "hooks": hooks,
                "per": int(per_webhook),
                "limit": int(limit),
            },
        )
        if not rows:
            return []
        tried = await ch.query_all(
            "SELECT event_id, argMax(attempts, version) AS attempts, "
            "argMax(last_status, version) AS last_status, "
            f"argMax(last_error, version) AS last_error FROM {ATTEMPTS} "
            "WHERE event_id IN {ids:Array(String)} GROUP BY event_id",
            params={"ids": [str(r["event_id"]) for r in rows]},
        )
    untried = {"attempts": 0, "last_status": 0, "last_error": ""}
    state = {str(t["event_id"]): t for t in tried}
    return [
        {
            **r,
            **{k: state.get(str(r["event_id"]), untried)[k] for k in untried},
        }
        for r in rows
    ]


async def mark_dead(
    now: datetime,
    *,
    since: datetime,
    hooks: list[Hook],
    reason: str,
    version: int,
    before: datetime | None = None,
    ch: _CHClient | None = None,
) -> None:
    """One statement: a `dead` attempt row with `reason` for every event of
    `hooks` since `since` (and before `before` when set) not yet delivered or
    dead."""
    if not hooks:
        return
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "now": now,
        "since": since,
        "hooks": hooks,
        "reason": reason,
        "version": int(version),
    }
    events = (
        f"SELECT event_id, occurred_at FROM {OUTBOX} WHERE (org, namespace, webhook_id) IN "
        f"{_HOOKS} AND occurred_at >= {{since:DateTime64(3)}}"
    )
    if before is not None:
        events += " AND occurred_at < {before:DateTime64(3)}"
        params["before"] = before
    events += f" {_EARLIEST}"
    # The dead row outranks what its SELECT missed: a delivery just written must be seen.
    with own_write_reads():
        await ch.execute(
            f"INSERT INTO {ATTEMPTS} ({', '.join(ATTEMPT_COLS)}) "
            "SELECT o.event_id, 'dead', ifNull(a.attempts, 0), {now:DateTime64(3)}, "
            "ifNull(a.last_status, 0), {reason:String}, {now:DateTime64(3)}, {version:UInt64} "
            f"FROM ({events}) AS o "
            f"LEFT JOIN ({_latest_attempts(f'SELECT event_id FROM ({events})')}) AS a "
            "ON a.event_id = o.event_id "
            "WHERE ifNull(a.event_id, '') = '' OR a.status IN ('pending', 'leased')",
            params=params,
            settings=_COMMITTED,
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
    with own_write_reads():  # an erasure that just finished must be seen
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
    set. Only the namespace's events are read (the outbox's sort-key prefix)."""
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "occurred_at >= {since:DateTime64(3)}",
    ]
    params: dict[str, Any] = {"org": org, "ns": namespace, "since": since, "limit": int(limit)}
    if webhook_id:
        where.append("webhook_id = {wid:String}")
        params["wid"] = webhook_id
    outer = ""
    if status:
        outer = "WHERE status = {status:String}"
        params["status"] = status
    scope = " AND ".join(where)
    cols = "event_id, webhook_id, event_type, memory_key, memory_id, memory_version, occurred_at"
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
            FROM (
                SELECT {cols} FROM {OUTBOX} WHERE {scope}
                {_EARLIEST}
            ) AS o
            LEFT JOIN ({_latest_attempts(f"SELECT event_id FROM {OUTBOX} WHERE {scope}")}) AS a
                ON a.event_id = o.event_id
        )
        {outer}
        ORDER BY occurred_at DESC
        LIMIT {{limit:UInt32}}
        """,
        params=params,
    )
