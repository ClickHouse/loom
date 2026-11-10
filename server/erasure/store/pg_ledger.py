"""loom.erasure_ledger: Postgres's record of the erasures requested of this deployment,
beside ClickHouse's own (`loom.erasure_requests`, `ledger.py`). After a restore of
either store, the erasures the other records as completed past the restore point run
again (`loom reconcile`, docs/operations/backup-and-recovery.md).

A saga's row is written before its first ClickHouse checkpoint, and gets `completed_at`
or `abandoned_at` with its terminal one. `backfill` copies in the erasures only
ClickHouse records (an earlier build's) and the terminal states this ledger missed.
Rows older than LOOM_ERASURE_LEDGER_DAYS are swept."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from server.core import pg
from server.core.datetimes import as_aware_utc, to_naive_utc
from server.core.repo_base import _CHClient, _default_client
from server.erasure.domain.failure_reason import ABANDONED_REASON_PREFIX
from server.erasure.store.ledger import LIVE_LEDGER
from server.settings import settings

_COLUMNS = (
    "org",
    "id",
    "scope_type",
    "scope_value",
    "purge",
    "requester",
    "urgency",
    "requested_at",
    "completed_at",
    "abandoned_at",
)
# ClickHouse's ledger rows in this ledger's shape: a terminal state only when it is one.
_CH_ROWS = (
    "SELECT org, id, scope_type, scope_value, purge, requester, toString(urgency) AS urgency, "
    "requested_at, "
    "if(status = 'completed', completed_at, NULL) AS completed_at, "
    "if(status = 'failed' AND startsWith(failure_reason, {abandoned:String}), completed_at, "
    "NULL) AS abandoned_at FROM loom.erasure_requests FINAL "
)
# A row still open here this long is checked against ClickHouse's ledger by `backfill`:
# a terminal state this ledger failed to record.
_OPEN_CHECK_AFTER = timedelta(minutes=10)


async def record(row: Mapping[str, Any]) -> None:
    """The ledger row of a saga being registered (`saga.begin_erasure`); a second
    registration of one id keeps the first."""
    await pg.execute(
        "INSERT INTO loom.erasure_ledger (org, id, scope_type, scope_value, purge, requester, "
        "urgency, requested_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
        "ON CONFLICT (org, id) DO NOTHING",
        row["org"],
        UUID(str(row["id"])),
        row["scope_type"],
        row["scope_value"],
        bool(row.get("purge")),
        str(row.get("requester") or ""),
        str(row.get("urgency") or "normal"),
        as_aware_utc(row["requested_at"]),
    )


async def mark_step(conn: Any, org: str, erasure_id: str, step: str) -> None:
    """Record on `conn`, in the transaction a Postgres step commits, that `step` of saga
    `erasure_id` ran: a copy restored from before that commit lacks it, so a replay runs
    it again, and one from after has it, so a replay leaves later writes alone."""
    await conn.execute(
        "UPDATE loom.erasure_ledger SET pg_steps = array_append(pg_steps, $3) "
        "WHERE org = $1 AND id = $2 AND NOT $3 = ANY(pg_steps)",
        org,
        UUID(erasure_id),
        step,
    )


async def unmark_steps(org: str, erasure_id: str, steps: Sequence[str]) -> None:
    """Drop `steps` from the row's `pg_steps`: a census reopened them, so until they
    commit again a replay after a restore runs them."""
    await pg.execute(
        "UPDATE loom.erasure_ledger SET pg_steps = ARRAY(SELECT unnest(pg_steps) "
        "EXCEPT SELECT unnest($3::text[])) WHERE org = $1 AND id = $2",
        org,
        UUID(erasure_id),
        list(steps),
    )


async def pg_steps(org: str, erasure_id: str) -> set[str]:
    """The Postgres steps saga `erasure_id`'s row records as committed (`mark_step`)."""
    steps = await pg.fetchval(
        "SELECT pg_steps FROM loom.erasure_ledger WHERE org = $1 AND id = $2",
        org,
        UUID(erasure_id),
    )
    return set(steps or [])


async def forget(org: str, erasure_id: str) -> None:
    """Delete the open row of a request that failed before its first checkpoint."""
    await pg.execute(
        "DELETE FROM loom.erasure_ledger WHERE org = $1 AND id = $2 "
        "AND completed_at IS NULL AND abandoned_at IS NULL",
        org,
        UUID(erasure_id),
    )


async def complete(org: str, erasure_id: str, at: datetime) -> None:
    """Mark a saga completed, at its terminal checkpoint's time."""
    await _end(org, erasure_id, "completed_at", at)


async def abandon(org: str, erasure_id: str, at: datetime) -> None:
    """Mark a saga abandoned (out of attempts): a reconcile never runs it again."""
    await _end(org, erasure_id, "abandoned_at", at)


async def _end(org: str, erasure_id: str, column: str, at: datetime) -> None:
    await pg.execute(
        f"UPDATE loom.erasure_ledger SET {column} = $3 "
        "WHERE org = $1 AND id = $2 AND completed_at IS NULL AND abandoned_at IS NULL",
        org,
        UUID(erasure_id),
        as_aware_utc(at),
    )


# Where `backfill` reads ClickHouse's ledger from next: (requested_at, id). It lives in
# this process, not in the store as worker cursors do, because losing it costs only a
# re-read: every copy is an idempotent upsert, so a restart, a lease handover or two
# copies at once walk the ledger again from its start and write the same rows.
_cursor: tuple[datetime, str] | None = None


def _move_cursor(rows: Sequence[Mapping[str, Any]], limit: int) -> None:
    """On past a full page; back to the start after a short one."""
    global _cursor
    _cursor = (rows[-1]["requested_at"], str(rows[-1]["id"])) if len(rows) == limit else None


async def backfill(limit: int, *, ch: _CHClient | None = None) -> int:
    """The next `limit` of ClickHouse's ledger rows (within LOOM_ERASURE_LEDGER_DAYS)
    copied in, with their terminal state: the erasures only ClickHouse records (an
    earlier build's, or one a process of the previous build ran during a rolling
    upgrade); then the terminal state of up to `limit` rows still open here after
    `_OPEN_CHECK_AFTER`, where ClickHouse records one. Returns how many rows it wrote."""
    ch = ch or _default_client()
    after_at, after_id = _cursor or (datetime(1970, 1, 1), "00000000-0000-0000-0000-000000000000")
    rows = await ch.query_all(
        _CH_ROWS
        + "WHERE requested_at > now() - toIntervalDay({days:UInt32}) AND "
        + LIVE_LEDGER
        + " AND (requested_at, id) > ({at:DateTime}, {id:UUID}) "
        "ORDER BY requested_at, id LIMIT {limit:UInt32}",
        params={
            "abandoned": ABANDONED_REASON_PREFIX,
            "days": settings.erasure_ledger_days,
            "at": to_naive_utc(after_at),
            "id": after_id,
            "limit": limit,
        },
    )
    _move_cursor(rows, limit)
    written = await _upsert(rows)
    open_ids = await pg.fetch(
        "SELECT id FROM loom.erasure_ledger WHERE completed_at IS NULL AND abandoned_at IS NULL "
        "AND requested_at < now() - $1::interval ORDER BY requested_at LIMIT $2",
        _OPEN_CHECK_AFTER,
        limit,
    )
    if open_ids:
        ended = await ch.query_all(
            _CH_ROWS
            + "WHERE id IN {ids:Array(UUID)} AND status IN ('completed', 'failed') AND "
            + LIVE_LEDGER,
            params={"abandoned": ABANDONED_REASON_PREFIX, "ids": [r["id"] for r in open_ids]},
        )
        written += await _upsert(ended)
    return written


async def _upsert(rows: Sequence[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    cols = [[r[c] for r in rows] for c in _COLUMNS]
    done = await pg.execute(
        "INSERT INTO loom.erasure_ledger AS l (org, id, scope_type, scope_value, purge, "
        "requester, urgency, requested_at, completed_at, abandoned_at) "
        "SELECT * FROM unnest($1::text[], $2::uuid[], $3::text[], $4::text[], $5::bool[], "
        "$6::text[], $7::text[], $8::timestamptz[], $9::timestamptz[], $10::timestamptz[]) "
        "ON CONFLICT (org, id) DO UPDATE SET completed_at = EXCLUDED.completed_at, "
        "abandoned_at = EXCLUDED.abandoned_at "
        "WHERE l.completed_at IS NULL AND l.abandoned_at IS NULL "
        "AND (EXCLUDED.completed_at IS NOT NULL OR EXCLUDED.abandoned_at IS NOT NULL)",
        cols[0],
        [UUID(str(v)) for v in cols[1]],
        cols[2],
        cols[3],
        [bool(v) for v in cols[4]],
        [str(v or "") for v in cols[5]],
        [str(v or "normal") for v in cols[6]],
        [as_aware_utc(v) for v in cols[7]],
        [as_aware_utc(v) for v in cols[8]],
        [as_aware_utc(v) for v in cols[9]],
    )
    return int(done.split()[-1])


async def completed_since(point: datetime) -> list[dict[str, Any]]:
    """The erasures that completed after `point`, oldest request first: those a restore
    of ClickHouse to `point` undid. From the point's whole second: a row copied from
    ClickHouse's ledger keeps whole seconds, and running again one that completed just
    before the point changes nothing its steps did not."""
    return await _rows("completed_at >= date_trunc('second', $1::timestamptz)", point)


async def still_open() -> list[dict[str, Any]]:
    """The erasures with no terminal state: a saga running, or awaiting its recovery."""
    return await _rows("completed_at IS NULL AND abandoned_at IS NULL")


async def _rows(where: str, *args: Any) -> list[dict[str, Any]]:
    rows = await pg.fetch(
        f"SELECT {', '.join(_COLUMNS)}, pg_steps FROM loom.erasure_ledger WHERE {where} "
        "ORDER BY requested_at, org, id",
        *[as_aware_utc(a) for a in args],
    )
    return [dict(r) for r in rows]


async def sweep(limit: int) -> int:
    """Delete up to `limit` rows requested more than LOOM_ERASURE_LEDGER_DAYS ago."""
    n = await pg.fetchval(
        "WITH gone AS (DELETE FROM loom.erasure_ledger WHERE ctid IN ("
        "  SELECT ctid FROM loom.erasure_ledger "
        "  WHERE requested_at < now() - make_interval(days => $1) "
        "  ORDER BY requested_at LIMIT $2 FOR UPDATE SKIP LOCKED) RETURNING 1) "
        "SELECT count(*) FROM gone",
        settings.erasure_ledger_days,
        limit,
    )
    return int(n)
