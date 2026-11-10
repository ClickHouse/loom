"""loom.knowledge_jobs: the knowledge ingestion jobs under the Postgres authority.

A job is one row, revised in place: `row` holds every column of
loom.knowledge_ingestions, and the queue's columns sit beside it. Once a version is
committed here it is also inserted into loom.knowledge_ingestions, which reports,
`knowledge.inspect` and a build that predates this table read. That projection is best
effort: each version carries the whole row, so the next save repairs a version that did
not land (and `ingest_run._take` projects an ended job's last one again), and a reader
that must not miss a job (the originals sweep, `ingest_jobs`) reads this table.

A run takes the job by raising its `lease_token`, and reads and saves it only under that
token: once another run of this build has taken the job, the run saves nothing more and
acts on no further read of it. The deployment-wide lease on the job's id
(`ingest_run.take_and_run`) keeps two runs apart, an older build's among them; the token
fences a run whose lease lapsed against a later taker of this build.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from server.core import pg
from server.knowledge.domain.owed_originals import OPEN_JOB
from server.knowledge.store import ingestions
from server.knowledge.store.ingestions import TIME_COLUMNS
from server.knowledge.store.pg_documents import keep_text, restore_text

log = logging.getLogger(__name__)

_KEY = "org = $1 AND namespace = $2 AND ingestion_id = $3"
# A job no run has saved for this long is expired, as loom.knowledge_ingestions' TTL
# drops it: `expire` deletes it, and until then no read offers or waits on it.
TTL = timedelta(days=30)  # not a setting: the ClickHouse table's TTL
_LIVE = f"updated_at >= now() - make_interval(days => {TTL.days})"
_EXPIRED = f"updated_at < now() - make_interval(days => {TTL.days})"
# The queue's columns, from the row.
_COLUMNS = (
    "org, namespace, ingestion_id, source_uri, state, next_attempt_at, created_at,"
    " updated_at, user_id, version, row"
)
_VALUES = "$1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb"
_INSERT = f"INSERT INTO loom.knowledge_jobs ({_COLUMNS}) VALUES ({_VALUES})"
# A version ClickHouse holds past the one held here: a build that predates this table
# ran the job.
_ADOPT = (
    "UPDATE loom.knowledge_jobs SET state = $4, next_attempt_at = $5, updated_at = $6,"
    f" version = $7, row = $8::jsonb WHERE {_KEY} AND version < $7"
)
_SAVE = (
    "UPDATE loom.knowledge_jobs SET state = $4, next_attempt_at = $5, updated_at = $6,"
    f" version = $7, row = $8::jsonb WHERE {_KEY} AND lease_token = $9 RETURNING 1"
)
# A job another transaction holds (an erasure deleting it) is skipped, not waited for.
_TAKE = (
    "UPDATE loom.knowledge_jobs SET lease_token = lease_token + 1"
    f" WHERE {_KEY} AND EXISTS (SELECT 1 FROM loom.knowledge_jobs WHERE {_KEY}"
    " FOR UPDATE SKIP LOCKED) RETURNING lease_token, row"
)
# One source's jobs run one at a time, oldest first; namespaces take turns, the one
# served longest ago first (`ingestions.due` describes the rules).
_DUE = f"""
WITH heads AS (
    SELECT DISTINCT ON (org, namespace, source_uri) org, namespace, state,
        next_attempt_at, updated_at, row
    FROM loom.knowledge_jobs WHERE {OPEN_JOB} AND {_LIVE}
    ORDER BY org, namespace, source_uri, created_at, ingestion_id
), due AS (
    SELECT DISTINCT ON (org, namespace) * FROM heads
    WHERE (state = 'pending' AND next_attempt_at <= now())
       OR (state = 'processing' AND updated_at <= now() - make_interval(secs => $1))
    ORDER BY org, namespace, state = 'processing' DESC, next_attempt_at
)
SELECT d.row FROM due d LEFT JOIN LATERAL (
    SELECT max(j.updated_at) AS last_served FROM loom.knowledge_jobs j
    WHERE j.org = d.org AND j.namespace = d.namespace AND j.state <> 'pending'
      AND j.updated_at > now() - interval '1 day'
) s ON true
ORDER BY s.last_served NULLS FIRST, d.next_attempt_at
LIMIT $2
"""
# As loom.knowledge_ingestions' TTL: a job no run has saved for 30 days.
_EXPIRE = (
    "WITH gone AS (DELETE FROM loom.knowledge_jobs WHERE ctid IN ("
    f"SELECT ctid FROM loom.knowledge_jobs WHERE {_EXPIRED}"
    " LIMIT $1) RETURNING 1) SELECT count(*) FROM gone"
)
_TOKEN_OF = f"SELECT lease_token FROM loom.knowledge_jobs WHERE {_KEY}"


def _encode(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v.isoformat() if k in TIME_COLUMNS else keep_text(v) for k, v in row.items()}


def _decode(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        k: datetime.fromisoformat(v) if k in TIME_COLUMNS else restore_text(v)
        for k, v in raw.items()
    }


def _at(row: dict[str, Any], column: str) -> datetime:
    at: datetime = row[column]
    return at.replace(tzinfo=UTC)  # a job's times are naive UTC


def _args(row: dict[str, Any]) -> list[Any]:
    return [
        row["org"],
        row["namespace"],
        row["ingestion_id"],
        row["source_uri"],
        row["state"],
        _at(row, "next_attempt_at"),
        _at(row, "created_at"),
        _at(row, "updated_at"),
        row["user_id"],
        int(row["version"]),
        _encode(row),
    ]


def _storable(org: str, namespace: str) -> bool:
    """Whether Postgres can hold a job of (org, namespace): a key it cannot store holds none."""
    return not pg.unstorable([org, namespace])


async def project(row: dict[str, Any]) -> None:
    """The committed version into loom.knowledge_ingestions."""
    try:
        await ingestions.insert_ingestion(row)
    except Exception as e:  # noqa: BLE001 — the version is committed; the next one repairs it
        log.warning(
            "knowledge_jobs: job %s version %s not projected: %s",
            row["ingestion_id"],
            row["version"],
            type(e).__name__,
        )


# Every write below commits in a transaction, acknowledged as durably as the standby
# allows (`server.core.pg`): an accepted job, a version, a take's token or a cancel
# that a failover lost would let a stale run's save through or drop the job.


async def _write(sql: str, *args: Any) -> Any:
    async with pg.transaction() as conn:
        return await conn.fetchrow(sql, *args)


async def insert(row: dict[str, Any]) -> None:
    """A new job, then its projection."""
    await _write(_INSERT, *_args(row))
    await project(row)


async def adopt(row: dict[str, Any]) -> None:
    """ClickHouse's version of a job held here at an older one. A job not held here is
    never inserted: one an erasure or the expiry deleted stays deleted."""
    await _write(
        _ADOPT,
        row["org"],
        row["namespace"],
        row["ingestion_id"],
        row["state"],
        _at(row, "next_attempt_at"),
        _at(row, "updated_at"),
        int(row["version"]),
        _encode(row),
    )


async def get(
    org: str, namespace: str, ingestion_id: str, *, durable: bool = False
) -> dict[str, Any] | None:
    """The job as Postgres holds it. `durable`: read in `pg.durable_transaction()`, so a
    version a cut-off commit left on the primary alone is not returned until the
    standbys hold it (what a projection to ClickHouse sends)."""
    if not _storable(org, namespace):
        return None
    sql = f"SELECT row FROM loom.knowledge_jobs WHERE {_KEY}"
    if durable:
        async with pg.durable_transaction() as conn:
            raw = await conn.fetchval(sql, org, namespace, ingestion_id)
    else:
        raw = await pg.fetchval(sql, org, namespace, ingestion_id)
    return None if raw is None else _decode(raw)


async def read(
    org: str, namespace: str, ingestion_id: str, token: int
) -> dict[str, Any] | Literal["taken", "gone"]:
    """The job as a run holding `token` reads it: `taken` when another run took it
    since, `gone` when an erasure or the expiry deleted it."""
    got = await pg.fetchrow(
        f"SELECT lease_token, row FROM loom.knowledge_jobs WHERE {_KEY}",
        org,
        namespace,
        ingestion_id,
    )
    if got is None:
        return "gone"
    return _decode(got["row"]) if int(got["lease_token"]) == token else "taken"


async def take(org: str, namespace: str, ingestion_id: str) -> tuple[int, dict[str, Any]] | None:
    """The job taken for a run: the token its saves carry and the job as it is now.
    None when no job of this id is held, or when another transaction holds its row (an
    erasure deleting it): the pass moves on. The caller found the job held (`get`)."""
    got = await _write(_TAKE, org, namespace, ingestion_id)
    return None if got is None else (int(got["lease_token"]), _decode(got["row"]))


async def save(row: dict[str, Any], token: int) -> Literal["saved", "taken", "gone"]:
    """A new version of the job under the run's `token`, then its projection: `taken`
    when another run took the job since, `gone` when an erasure or the expiry deleted
    it."""
    key = (row["org"], row["namespace"], row["ingestion_id"])
    saved = await _write(
        _SAVE,
        *key,
        row["state"],
        _at(row, "next_attempt_at"),
        _at(row, "updated_at"),
        int(row["version"]),
        _encode(row),
        token,
    )
    if saved is None:
        return "gone" if await pg.fetchval(_TOKEN_OF, *key) is None else "taken"
    await project(row)
    return "saved"


async def earlier_open(row: dict[str, Any]) -> bool:
    """Whether an open job of `row`'s source held here was created before it. A key
    Postgres cannot store holds no job here (an older build queued it)."""
    if pg.unstorable([row["org"], row["namespace"], row["source_uri"]]):
        return False
    hit = await pg.fetchval(
        "SELECT 1 FROM loom.knowledge_jobs WHERE org = $1 AND namespace = $2"
        f" AND source_uri = $3 AND {OPEN_JOB} AND {_LIVE}"
        " AND (created_at, ingestion_id) < ($4, $5) LIMIT 1",
        row["org"],
        row["namespace"],
        row["source_uri"],
        _at(row, "created_at"),
        row["ingestion_id"],
    )
    return hit is not None


async def held_ids(org: str, namespace: str, ids: list[str]) -> set[str]:
    """Which of `ids` are jobs held here."""
    if not ids or not _storable(org, namespace):
        return set()
    rows = await pg.fetch(
        "SELECT ingestion_id FROM loom.knowledge_jobs WHERE org = $1 AND namespace = $2"
        " AND ingestion_id = ANY($3::text[])",
        org,
        namespace,
        ids,
    )
    return {str(r["ingestion_id"]) for r in rows}


async def request_cancel(org: str, namespace: str, ingestion_id: str) -> None:
    if _storable(org, namespace):
        await _write(
            "UPDATE loom.knowledge_jobs SET cancel_requested_at ="
            f" COALESCE(cancel_requested_at, now()) WHERE {_KEY}",
            org,
            namespace,
            ingestion_id,
        )


async def cancel_requested(org: str, namespace: str, ingestion_id: str) -> bool:
    if not _storable(org, namespace):
        return False
    asked = await pg.fetchval(
        f"SELECT cancel_requested_at IS NOT NULL FROM loom.knowledge_jobs WHERE {_KEY}",
        org,
        namespace,
        ingestion_id,
    )
    return bool(asked)


async def due(limit: int, *, stale_after_s: float) -> list[dict[str, Any]]:
    """The worker's queue across tenants, as `ingestions.due` orders it."""
    rows = await pg.fetch(_DUE, float(stale_after_s), int(limit))
    return [_decode(r["row"]) for r in rows]


async def expire(limit: int) -> int:
    """Delete up to `limit` jobs no run has saved for 30 days; returns how many."""
    got = await _write(_EXPIRE, int(limit))
    return int(got[0])
