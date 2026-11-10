"""loom.knowledge_datasets: the knowledge dataset catalog under the Postgres authority.

A catalog row says which snapshot serves a dataset of a source at a document version, and
its schema and approval. Each row here holds every column of loom.datasets, revised in
place: a write lands only over an older `version`, as ReplacingMergeTree(version)
decides. `datasets_repo` projects each committed row into loom.datasets, best effort.

An approval commits in the transaction that reads the source's head (`approve`), so it
lands only while the document it reviewed still serves.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from server.core import pg
from server.knowledge.store.pg_documents import ERASED, keep_text, restore_text
from server.settings import settings

# The catalog's times, kept to the millisecond as DateTime64(3) stores them
# (`datasets_repo.dataset_row`) and as ISO text in `row`.
TIME_COLUMNS = ("approved_at", "created_at", "updated_at")

# A catalog row, inserted or written over its key's row when the WHERE that follows allows.
_WRITE = (
    "INSERT INTO loom.knowledge_datasets"
    " (org, namespace, source_uri, doc_version, dataset_id, snapshot_id, version, user_id, row)"
    " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)"
    " ON CONFLICT (org, namespace, source_uri, doc_version, dataset_id) DO UPDATE SET"
    " snapshot_id = EXCLUDED.snapshot_id, version = EXCLUDED.version,"
    " user_id = EXCLUDED.user_id, row = EXCLUDED.row"
)
# Over an older version only, as ReplacingMergeTree(version) decides.
_UPSERT = f"{_WRITE} WHERE knowledge_datasets.version < EXCLUDED.version RETURNING 1"
# The approval: over the version of the row it read ($10), or no row, never a later one.
_APPROVE = f"{_WRITE} WHERE knowledge_datasets.version <= $10 RETURNING 1"
# The publishing job, held against an erasure's delete for the publish's transaction.
_JOB = (
    "SELECT 1 FROM loom.knowledge_jobs WHERE org = $1 AND namespace = $2"
    " AND ingestion_id = $3 FOR SHARE"
)
# The source's head, held against a commit for the approval's transaction.
_HEAD = (
    "SELECT version, deleted FROM loom.knowledge_heads"
    " WHERE org = $1 AND namespace = $2 AND source_uri = $3 FOR SHARE"
)


def _encode(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if k in TIME_COLUMNS:
            out[k] = v.isoformat() if isinstance(v, datetime) else None
        else:
            out[k] = keep_text(v)
    return out


def _decode(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (datetime.fromisoformat(v) if v is not None else None)
        if k in TIME_COLUMNS
        else restore_text(v)
        for k, v in raw.items()
    }


def _args(row: dict[str, Any]) -> list[Any]:
    return [
        row["org"],
        row["namespace"],
        row["source_uri"],
        int(row["doc_version"]),
        row["dataset_id"],
        int(row["snapshot_id"]),
        int(row["version"]),
        row["user_id"],
        _encode(row),
    ]


def _storable(*keys: str) -> bool:
    """Whether Postgres can hold rows of these keys: one it cannot store holds none."""
    return not pg.unstorable(list(keys))


def storable(row: dict[str, Any]) -> bool:
    """Whether Postgres can hold the catalog row `row`: a key or uploader it cannot store
    (a source written before its namespace had heads) keeps it in ClickHouse alone."""
    keys = ("org", "namespace", "source_uri", "dataset_id", "user_id")
    return _storable(*(str(row[k]) for k in keys))


class _Moved(Exception):
    """A refused approval, raised in its transaction so that it rolls back: a commit of
    its locks would wait for the standby and could answer 503 for a refusal."""


class JobErased(Exception):
    """The ingest job a publish writes for is gone: an erasure deleted it."""


async def upsert(
    rows: Sequence[dict[str, Any]], *, job: tuple[str, str, str] | None = None
) -> list[dict[str, Any]]:
    """Commit each catalog row over an older version of its key (a newer one stays) and
    return the rows written, the only ones to project. Every row is `storable`. `job` is
    the (org, namespace, ingestion_id) of the ingest publishing them, held in
    `loom.knowledge_jobs`: its row is read in the same transaction, and JobErased raised
    with nothing written when an erasure deleted it. An erasure deletes jobs before
    catalog rows, so one committed first leaves no job, and one that comes after waits
    for this transaction and then deletes what it wrote."""
    async with pg.transaction() as conn:
        if job is not None and not await conn.fetchval(_JOB, *job):
            raise JobErased(job[2])
        return [r for r in rows if await conn.fetchval(_UPSERT, *_args(r)) is not None]


async def at_version(
    org: str, namespace: str, source_uri: str, doc_version: int, *, dataset_id: str = ""
) -> list[dict[str, Any]]:
    """The source's datasets at `doc_version` (one of them with `dataset_id`), in the
    artifact's table order."""
    if not _storable(org, namespace, source_uri, dataset_id):
        return []
    one = " AND dataset_id = $5" if dataset_id else ""
    args: list[Any] = [org, namespace, source_uri, int(doc_version)]
    if dataset_id:
        args.append(dataset_id)
    rows = await pg.fetch(
        "SELECT row FROM loom.knowledge_datasets WHERE org = $1 AND namespace = $2"
        f" AND source_uri = $3 AND doc_version = $4{one}"
        " ORDER BY (row->>'position')::int, dataset_id LIMIT 10000",
        *args,
    )
    return [_decode(r["row"]) for r in rows]


async def approve(new: dict[str, Any], *, read: int, doc_id: str) -> Literal["switched", "moved"]:
    """Commit the approval `new` unless the source's head has moved past the document
    version it reviewed (a later version, or that version deleted), an erasure removed
    that document (`doc_id`, the reviewed row's), or a write of the catalog row followed
    version `read`, the one the approver read; `moved` then, nothing written. A source
    with no head (written before its namespace had heads), or a head an older build left
    behind ClickHouse's row, is not refused for it: the source's claim keeps a publish
    out. A row an older build wrote to ClickHouse alone is absent here or older than
    `read`, so the approval commits it. One transaction, under the namespace's lock a
    document commit shares and an erasure holds alone, the head held against a commit."""
    org, namespace, uri = new["org"], new["namespace"], new["source_uri"]
    reviewed = int(new["doc_version"])
    try:
        async with pg.transaction() as conn:
            await conn.execute(
                f"SET LOCAL lock_timeout = {int(settings.pg_commit_lock_timeout_ms)}"
            )
            await conn.execute(
                f"SELECT pg_advisory_xact_lock_shared({pg.KNOWLEDGE_LOCK_KEY})", org, namespace
            )
            head = await conn.fetchrow(_HEAD, org, namespace, uri)
            if head is not None and (
                int(head["version"]) > reviewed
                or (int(head["version"]) == reviewed and head["deleted"])
            ):
                raise _Moved
            # Read after the head's lock and the namespace's, which an erasure holds until
            # it commits: one that ran since the read refuses the approval.
            if await conn.fetchval(
                ERASED, org, namespace, [doc_id], settings.pg_erasure_evidence_hours
            ):
                raise _Moved
            if await conn.fetchval(_APPROVE, *_args(new), int(read)) is None:
                raise _Moved
    except _Moved:
        return "moved"
    return "switched"


async def forget(
    org: str, namespace: str, source_uris: Sequence[str], *, before: int = 0, only: int = 0
) -> set[str]:
    """Delete the sources' catalog rows of document versions older than `before`, or of
    version `only` (`datasets_repo.forget`); returns the ids of the datasets they named."""
    uris = [u for u in source_uris if _storable(u)]
    if not uris or not _storable(org, namespace):
        return set()
    which = "doc_version < $4" if before else "doc_version = $4"
    # In a transaction, so that a commit the standby's wait leaves unknown says so.
    async with pg.transaction() as conn:
        gone = await conn.fetch(
            "DELETE FROM loom.knowledge_datasets WHERE org = $1 AND namespace = $2"
            f" AND source_uri = ANY($3::text[]) AND {which} RETURNING dataset_id",
            org,
            namespace,
            uris,
            int(before or only),
        )
    return {str(r["dataset_id"]) for r in gone}


async def forget_published(
    org: str, namespace: str, through: dict[str, int]
) -> tuple[set[str], set[int]]:
    """Delete each source's catalog rows up to `through[source_uri]`
    (`datasets_repo.forget_published`); returns the ids of the datasets they named, and the
    snapshot ids the sources' later rows still serve, whose rows stay."""
    uris = sorted(u for u in through if _storable(u))
    if not uris or not _storable(org, namespace):
        return set(), set()
    bounds = [int(through[u]) for u in uris]
    async with pg.transaction() as conn:
        kept = await conn.fetch(
            "SELECT DISTINCT d.snapshot_id FROM loom.knowledge_datasets d"
            " JOIN unnest($3::text[], $4::bigint[]) AS t(uri, upto) ON d.source_uri = t.uri"
            " WHERE d.org = $1 AND d.namespace = $2 AND d.doc_version > t.upto",
            org,
            namespace,
            uris,
            bounds,
        )
        gone = await conn.fetch(
            "DELETE FROM loom.knowledge_datasets d USING unnest($3::text[], $4::bigint[])"
            " AS t(uri, upto) WHERE d.org = $1 AND d.namespace = $2"
            " AND d.source_uri = t.uri AND d.doc_version <= t.upto RETURNING d.dataset_id",
            org,
            namespace,
            uris,
            bounds,
        )
    return {str(r["dataset_id"]) for r in gone}, {int(r["snapshot_id"]) for r in kept}
