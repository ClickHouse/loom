"""loom.knowledge_uploads and loom.knowledge_ingestions: verified uploads and the
jobs that extract (through the extractor service), chunk, embed and publish them.

An upload is one row, written once after its bytes are stored and checked; it
holds the original from the sweep until `expires_at`. An ingestion is a
ReplacingMergeTree(version) row per state change, read with FINAL; every new
version carries every column forward (`ingestion_row`).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core.repo_base import _CHClient, _default_client

UPLOADS = "loom.knowledge_uploads"
INGESTIONS = "loom.knowledge_ingestions"
CANCELS = "loom.knowledge_ingest_cancels"

# An ingestion's states: the first two are open (a worker may take the job), the
# rest are where it ended.
OPEN_STATES = ("pending", "processing")
DONE_STATES = ("ready", "partial", "failed", "cancelled")
# A processing job's stages, in order (`stage`; empty once it ends).
PARSE, EMBED, PUBLISH = "parse", "embed", "publish"

INGESTION_COLUMNS = (
    "org",
    "namespace",
    "ingestion_id",
    "source_uri",
    "upload_id",
    "sha256",
    "filename",
    "content_type",
    "title",
    "description",
    "tags",
    "metadata",
    "options",
    "expected_revision",
    "allow_partial",
    "state",
    "stage",
    "attempts",
    "next_attempt_at",
    "error_code",
    "error_message",
    "format",
    "coverage",
    "warnings",
    "artifact_sha256",
    "chunks",
    "revision",
    # The chunk version a run inserted before its document row: retired unless
    # the document switched to it (a publish that stopped halfway).
    "doc_version",
    "agent",
    "user_id",
    "session_id",
    "actor",
    "source_class",
    "created_at",
    "updated_at",
    "version",
)

_DEFAULTS: dict[str, Any] = {
    "filename": "",
    "content_type": "",
    "title": "",
    "description": "",
    "tags": [],
    "metadata": "{}",
    "options": "{}",
    "expected_revision": -1,
    "allow_partial": False,
    "stage": "",
    "attempts": 0,
    "error_code": "",
    "error_message": "",
    "format": "",
    "coverage": "",
    "warnings": [],
    "artifact_sha256": "",
    "chunks": 0,
    "revision": 0,
    "doc_version": 0,
    "agent": "-",
    "user_id": "-",
    "session_id": "-",
    "actor": "",
    "source_class": "",
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def ingestion_row(existing: dict[str, Any] | None = None, /, **fields: Any) -> dict[str, Any]:
    """The one way to build a loom.knowledge_ingestions row: the schema
    defaults, then every column of `existing`, then `fields`, with a fresh
    `version` and `updated_at`. A version row that left a column out would
    reset it for every FINAL reader."""
    unknown = set(fields) - set(INGESTION_COLUMNS)
    if unknown:
        raise ValueError(f"ingestion_row: not a column: {sorted(unknown)}")
    now = _now()
    row: dict[str, Any] = {**_DEFAULTS, "created_at": now, "next_attempt_at": now}
    if existing is not None:
        row.update({c: existing[c] for c in INGESTION_COLUMNS if c in existing})
    row.update(fields)
    row["updated_at"] = now
    row["version"] = max(time.time_ns(), int(row.get("version") or 0) + 1)
    missing = [c for c in INGESTION_COLUMNS if c not in row]
    if missing:
        raise ValueError(f"ingestion_row: missing {missing}")
    return {c: row[c] for c in INGESTION_COLUMNS}


async def insert_upload(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(UPLOADS, [row], settings={"async_insert": 0})


def upload_row(
    *,
    org: str,
    namespace: str,
    sha256: str,
    size: int,
    filename: str,
    content_type: str,
    fmt: str,
    author: dict[str, str],
    ttl_s: int,
) -> dict[str, Any]:
    now = _now()
    return {
        "org": org,
        "namespace": namespace,
        "upload_id": str(uuid.uuid4()),
        "sha256": sha256,
        "size": size,
        "filename": filename,
        "content_type": content_type,
        "format": fmt,
        "agent": author["agent"],
        "user_id": author["user_id"],
        "session_id": author["session_id"],
        "actor": author["actor"],
        "source_class": author["source_class"],
        "created_at": now,
        "expires_at": now + timedelta(seconds=ttl_s),
    }


async def get_upload(
    org: str, namespace: str, upload_id: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT upload_id, toString(sha256) AS sha256, size, filename,"
        " toString(content_type) AS content_type, toString(format) AS format, agent, user_id,"
        f" actor, created_at, expires_at FROM {UPLOADS}"
        " WHERE org = {org:String} AND namespace = {ns:String} AND upload_id = {id:UUID} LIMIT 1",
        params={"org": org, "ns": namespace, "id": upload_id},
    )
    return {**row, "upload_id": str(row["upload_id"])} if row else None


async def insert_cancel(
    org: str,
    namespace: str,
    ingestion_id: str,
    author: dict[str, str],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Record a cancel of the job: a row of its own, so it never replaces a
    version of the job the worker saved after the caller read it."""
    ch = ch or _default_client()
    row = {
        "org": org,
        "namespace": namespace,
        "ingestion_id": ingestion_id,
        **{c: author[c] for c in ("agent", "user_id", "session_id", "actor")},
        "requested_at": _now(),
    }
    await ch.insert(CANCELS, [row], settings={"async_insert": 0})


async def cancel_requested(
    org: str, namespace: str, ingestion_id: str, *, ch: _CHClient | None = None
) -> bool:
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT 1 AS hit FROM {CANCELS} WHERE org = {{org:String}} AND namespace = {{ns:String}}"
        " AND ingestion_id = {id:UUID} LIMIT 1",
        params={"org": org, "ns": namespace, "id": ingestion_id},
    )
    return row is not None


async def insert_ingestion(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert(INGESTIONS, [row], settings={"async_insert": 0})


_SELECT = (
    # The filtered columns (ingestion_id, state) are selected as stored: an alias
    # of the same name would shadow the column in WHERE. `_job` makes the id a string.
    "SELECT org, namespace, ingestion_id, source_uri,"
    " toString(upload_id) AS upload_id, toString(sha256) AS sha256, filename,"
    " toString(content_type) AS content_type, title, description, tags, metadata, options,"
    " expected_revision, allow_partial, state, toString(stage) AS stage,"
    " attempts, next_attempt_at, toString(error_code) AS error_code, error_message,"
    " toString(format) AS format, coverage, warnings, artifact_sha256, chunks, revision,"
    " doc_version, agent, user_id, session_id, actor,"
    " toString(source_class) AS source_class,"
    f" created_at, updated_at, version FROM {INGESTIONS} FINAL"
)


def _job(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "ingestion_id": str(row["ingestion_id"]), "state": str(row["state"])}


async def get_ingestion(
    org: str, namespace: str, ingestion_id: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    ch = ch or _default_client()
    row = await ch.query_one(
        f"{_SELECT} WHERE org = {{org:String}} AND namespace = {{ns:String}}"
        " AND ingestion_id = {id:UUID} LIMIT 1",
        params={"org": org, "ns": namespace, "id": ingestion_id},
    )
    return _job(row) if row else None


async def due(
    limit: int, *, stale_after_s: float, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The worker's queue across tenants: pending jobs whose next attempt is
    due, then processing jobs no worker has updated for `stale_after_s` (their
    worker is likely gone; the lease decides). A job a live worker is running is
    left out, so running jobs never fill a pass.

    Fair across namespaces: each contributes its oldest due job, and the
    namespace whose last job started longest ago (or never, within a day) comes
    first, so one namespace's backlog takes turns with every other's instead of
    running ahead of them."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT c.* FROM ({_SELECT} WHERE (state = 'pending' AND next_attempt_at <= now64(3))"
        " OR (state = 'processing'"
        " AND updated_at <= now64(3) - toIntervalMillisecond({stale:UInt64}))"
        " ORDER BY state = 'processing', next_attempt_at LIMIT 1 BY org, namespace) AS c"
        " LEFT JOIN (SELECT org, namespace, max(updated_at) AS last_started"
        f" FROM {INGESTIONS} FINAL WHERE state != 'pending'"
        " AND updated_at > now64(3) - INTERVAL 1 DAY GROUP BY org, namespace) AS s"
        " ON c.org = s.org AND c.namespace = s.namespace"
        # ifNull: with join_use_nulls a namespace never served reads NULL, which sorts last.
        " ORDER BY c.state = 'processing', ifNull(s.last_started, toDateTime64(0, 3)),"
        " c.next_attempt_at"
        " LIMIT {lim:UInt32}",
        params={"stale": int(stale_after_s * 1000), "lim": int(limit)},
    )
    return [_job(r) for r in rows]


# The originals the namespace's unexpired uploads and open ingestions need.
HELD_SQL = (
    f"SELECT toString(sha256) AS sha FROM {UPLOADS}"
    " WHERE org = {org:String} AND namespace = {ns:String} AND expires_at > now64(3)"
    " UNION DISTINCT"
    f" SELECT toString(sha256) AS sha FROM {INGESTIONS} FINAL"
    " WHERE org = {org:String} AND namespace = {ns:String} AND state IN ('pending', 'processing')"
)
