"""loom.memory_webhooks and loom.memory_webhook_outbox: every statement of
memory webhooks.

Both are ReplacingMergeTree(version) read with FINAL. A registration is
rewritten whole (`WEBHOOK_COLS`); an outbox row is rewritten whole by the
delivery worker, once per lease and once per attempt (`OUTBOX_COLS`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.ch import own_write_reads
from server.core.repo_base import _CHClient, _default_client

WEBHOOKS = "loom.memory_webhooks"
OUTBOX = "loom.memory_webhook_outbox"

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
    "status",
    "attempts",
    "next_try_at",
    "lease_until",
    "last_status",
    "last_error",
    "version",
)

_COMMITTED = {"async_insert": 0, "wait_for_async_insert": 1}


def _whole(row: dict[str, Any], cols: tuple[str, ...]) -> dict[str, Any]:
    """`row` with exactly `cols`: a missing column raises rather than landing
    its DEFAULT over the value the previous version held."""
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


async def due_outbox(
    now: datetime, *, limit: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Pending rows whose next try is due and whose lease has run out, oldest
    try first, at most `limit`."""
    ch = ch or _default_client()
    with own_write_reads():  # a lease just written must be seen
        return await ch.query_all(
            f"SELECT {', '.join(OUTBOX_COLS)} FROM {OUTBOX} FINAL "
            "WHERE status = 'pending' AND next_try_at <= {now:DateTime64(3)} "
            "AND lease_until <= {now:DateTime64(3)} "
            "ORDER BY next_try_at LIMIT {limit:UInt32}",
            params={"now": now, "limit": int(limit)},
        )


async def namespace_deliveries(
    org: str,
    namespace: str,
    *,
    webhook_id: str,
    status: str,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The namespace's outbox rows, newest event first; `webhook_id` and
    `status` narrow it when set."""
    ch = ch or _default_client()
    where = ["org = {org:String}", "namespace = {ns:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace, "limit": int(limit)}
    if webhook_id:
        where.append("webhook_id = {wid:String}")
        params["wid"] = webhook_id
    if status:
        where.append("status = {status:String}")
        params["status"] = status
    return await ch.query_all(
        f"SELECT {', '.join(OUTBOX_COLS)} FROM {OUTBOX} FINAL WHERE {' AND '.join(where)} "
        "ORDER BY occurred_at DESC LIMIT {limit:UInt32}",
        params=params,
    )
