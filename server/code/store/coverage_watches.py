"""SQL for `loom.code_coverage_watches` and the one `task_events` row a watch
posts when its commit is covered.

The watch table is a ReplacingMergeTree by `version`: every write carries
every column, a read is `FINAL … is_deleted = 0` under
`select_sequential_consistency` (a watch written a moment ago on another
replica must be seen by the next worker pass).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from server.code.domain.coverage_watch import CoverageWatch
from server.core.repo_base import _CHClient, _default_client

WATCH_COLS = (
    "org, namespace, commit_sha, inbox, created_by, created_at, delivered_at, is_deleted, version"
)
WATCH_FIELDS: tuple[str, ...] = tuple(c.strip() for c in WATCH_COLS.split(","))

READY_EVENT = "code.coverage.ready"
_EVENT_ID_NS = uuid.UUID("5b0e7f6e-3c2a-4b8e-9d1f-6a0c2e4d8b17")
_INSERT_SETTINGS: dict[str, Any] = {"async_insert": 0, "wait_for_async_insert": 1}


def event_id(org: str, namespace: str, commit_sha: str, inbox: str) -> str:
    """The ready event's task_id: one per (org, namespace, commit, inbox), so a
    re-run finds the event it already wrote instead of writing a second."""
    return str(uuid.uuid5(_EVENT_ID_NS, f"{org}\x1f{namespace}\x1f{commit_sha}\x1f{inbox}"))


def _watch(row: dict[str, Any]) -> CoverageWatch:
    return CoverageWatch(
        org=str(row["org"]),
        namespace=str(row["namespace"]),
        commit_sha=str(row["commit_sha"]),
        inbox=str(row["inbox"]),
        created_by=str(row.get("created_by") or ""),
        created_at=row["created_at"],
        delivered_at=row.get("delivered_at"),
        is_deleted=int(row.get("is_deleted") or 0),
        version=int(row["version"]),
    )


async def fetch_watch(
    org: str, namespace: str, commit_sha: str, inbox: str, *, ch: _CHClient | None = None
) -> CoverageWatch | None:
    ch = ch or _default_client()
    row = await ch.query_one(
        f"SELECT {WATCH_COLS} FROM loom.code_coverage_watches FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND commit_sha = {sha:String} AND inbox = {inbox:String} AND is_deleted = 0 "
        "LIMIT 1 SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": namespace, "sha": commit_sha, "inbox": inbox},
    )
    return _watch(row) if row else None


async def pending_watches(*, limit: int = 5000, ch: _CHClient | None = None) -> list[CoverageWatch]:
    """Every live watch not yet delivered, org-wide."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        f"SELECT {WATCH_COLS} FROM loom.code_coverage_watches FINAL "
        "WHERE is_deleted = 0 AND delivered_at IS NULL "
        "ORDER BY org, namespace, commit_sha, inbox LIMIT {limit:UInt32} "
        "SETTINGS select_sequential_consistency = 1",
        params={"limit": int(limit)},
    )
    return [_watch(r) for r in rows]


async def insert_watch_version(watch: CoverageWatch, *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    row = dataclasses.asdict(watch)
    await ch.insert("loom.code_coverage_watches", [row], settings=dict(_INSERT_SETTINGS))


async def ready_event_exists(
    org: str, inbox: str, task_id: str, *, ch: _CHClient | None = None
) -> bool:
    ch = ch or _default_client()
    row = await ch.query_one(
        "SELECT count() AS n FROM loom.task_events "
        "WHERE org = {org:String} AND inbox = {inbox:String} AND task_id = {tid:UUID} "
        "AND event = {event:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "inbox": inbox, "tid": task_id, "event": READY_EVENT},
    )
    return bool(row and int(row.get("n") or 0) > 0)


async def insert_ready_event(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """The event row; the id doubles as the insert's dedup token, so a retried
    insert of the same row lands once."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.task_events",
        [row],
        settings={**_INSERT_SETTINGS, "insert_deduplication_token": str(row["task_id"])},
    )
