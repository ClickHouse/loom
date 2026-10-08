"""loom.namespace_write_marks — when a namespace's planes last moved.

One row per (org, namespace, plane), kept at the max write time. The
ClickHouse client feeds an in-process buffer after every INSERT into a plane
table (`observe_insert`); the buffer flushes as one INSERT every
`namespace_write_marks_flush_interval_s`, rather than one part per source
insert.

`last_writes` answers "has anything been written to these namespaces since
T" with one primary-key read, folding in this process's unflushed marks so a
process reads its own writes at once; another process sees them after the
flush (at most the interval, five seconds by default — the precision cost of
the buffer, against one part per write). `bump` records a write no INSERT
into a plane table carries (a mutation, such as an erasure). The org-wide
sentinel namespace `*` marks writes whose namespace is not known at the call
site; every reader folds it in.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from server.core.background import TaskSet
from server.core.datetimes import as_aware_utc as _utc
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client

log = logging.getLogger("loom.namespace_write_marks")

TABLE = "loom.namespace_write_marks"
ORG_WIDE = "*"

# The plane each source table marks.
PLANE_BY_TABLE: dict[str, str] = {
    "loom.memory": "memory",
    "loom.memory_chunks": "chunk",
    "loom.memory_evidence": "evidence",
    "loom.memory_records": "record",
    "loom.session_summaries": "summary",
}

_pending: dict[tuple[str, str, str], datetime] = {}
_timer: asyncio.TimerHandle | None = None
_inflight = TaskSet()


def _interval_s() -> float:
    from server.settings import settings

    return max(0.05, float(settings.namespace_write_marks_flush_interval_s))


def observe_insert(table: str, rows: list[dict[str, Any]]) -> None:
    """Called by the client after a successful INSERT: one mark per distinct
    (org, namespace) in the batch for the table's plane. Tables outside the
    five planes, and the buffer's own flush, are ignored."""
    plane = PLANE_BY_TABLE.get(table)
    if plane is None or not rows:
        return
    now = naive_utc_now()
    seen: set[tuple[str, str]] = set()
    for r in rows:
        org = str(r.get("org") or "")
        if not org:
            continue
        ns = str(r.get("namespace") or "") or ORG_WIDE
        if (org, ns) in seen:
            continue
        seen.add((org, ns))
        touch(org, ns, plane, at=now)


def touch(org: str, namespace: str, plane: str, *, at: datetime | None = None) -> None:
    """Record a write in the buffer; the flush lands it within the interval."""
    at = at or naive_utc_now()
    key = (org, namespace or ORG_WIDE, plane)
    prev = _pending.get(key)
    if prev is None or at > prev:
        _pending[key] = at
    _arm()


def _arm() -> None:
    global _timer
    if _timer is not None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (sync caller): flushed by an explicit flush_now()
    _timer = loop.call_later(_interval_s(), _fire)


def _fire() -> None:
    global _timer
    _timer = None
    try:
        _inflight.spawn(_timed_flush(), name="loom-write-marks-flush", detached=True)
    except RuntimeError:
        return


# The timer's flush runs in a detached (empty) context; the tag names its insert
# in the query log instead of the `untagged:` frame fallback.
FLUSH_QUERY_TAG = "namespace_write_marks.flush"


async def _timed_flush() -> int:
    from server.core.ch import set_query_tag

    set_query_tag(FLUSH_QUERY_TAG)
    return await flush_now()


async def flush_now(*, ch: _CHClient | None = None) -> int:
    """Write every buffered mark as one INSERT. Returns rows written.

    A mark leaves the buffer only once the insert has landed, and only if no
    newer write moved it meanwhile: until then `last_writes` still folds it
    in, and a failed flush keeps it and re-arms the timer. A precedent reuse
    reads these marks to prove nothing was written since, so a dropped mark
    would serve a stale answer."""
    if not _pending:
        return 0
    items = list(_pending.items())
    rows = [
        {"org": org, "namespace": ns, "plane": plane, "last_write": ts}
        for (org, ns, plane), ts in items
    ]
    try:
        ch = ch or _default_client()
        await ch.insert(TABLE, rows)
    except Exception as e:  # noqa: BLE001 — telemetry-grade write; the marks stay buffered
        log.warning("namespace_write_marks flush failed (non-fatal, %d rows): %s", len(rows), e)
        _arm()
        return 0
    for key, ts in items:
        if _pending.get(key) == ts:
            del _pending[key]
    return len(rows)


def pending() -> dict[tuple[str, str, str], datetime]:
    return dict(_pending)


def _reset_for_tests() -> None:
    global _timer
    _pending.clear()
    if _timer is not None:
        _timer.cancel()
        _timer = None


async def last_writes(
    org: str,
    namespaces: list[str],
    *,
    ch: _CHClient | None = None,
) -> dict[str, datetime]:
    """namespace → latest write across its planes, for the namespaces asked
    plus the org-wide sentinel. A namespace absent from the result has no
    recorded write since the marks began. This process's unflushed marks
    are folded in."""
    if not namespaces:
        return {}
    ch = ch or _default_client()
    wanted = sorted({*namespaces, ORG_WIDE})
    rows = await ch.query_all(
        f"""
        SELECT namespace, max(last_write) AS last_write
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace IN {{nss:Array(String)}}
        GROUP BY namespace
        """,
        params={"org": org, "nss": wanted},
    )
    out: dict[str, datetime] = {}
    for r in rows:
        ts = _utc(r.get("last_write"))
        if ts is not None:
            out[str(r.get("namespace") or "")] = ts
    wanted_set = set(wanted)
    for (p_org, p_ns, _plane), at in _pending.items():
        if p_org != org or p_ns not in wanted_set:
            continue
        ts = _utc(at)
        if ts is not None and (p_ns not in out or ts > out[p_ns]):
            out[p_ns] = ts
    return out


async def blacklist_updated_at(*, ch: _CHClient | None = None) -> datetime | None:
    """When the blacklist last changed (the 1-row version watermark). A
    blacklist edit filters search results, so it moves every window."""
    ch = ch or _default_client()
    row = await ch.query_one("SELECT max(updated_at) AS updated_at FROM loom.blacklist_version")
    return _utc((row or {}).get("updated_at"))


async def bump(
    org: str,
    namespace: str,
    plane: str,
    *,
    ch: _CHClient | None = None,
) -> None:
    """Record a write no plane-table INSERT carries. Best-effort: a failed bump is
    logged, never raised — the caller is on an erasure or a write path."""
    ch = ch or _default_client()
    try:
        await ch.insert(
            TABLE,
            [
                {
                    "org": org,
                    "namespace": namespace or ORG_WIDE,
                    "plane": plane,
                    "last_write": naive_utc_now(),
                }
            ],
        )
    except Exception as e:  # noqa: BLE001 — telemetry-grade write
        log.warning("namespace_write_marks bump failed (%s/%s/%s): %s", org, namespace, plane, e)
