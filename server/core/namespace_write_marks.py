"""loom.namespace_write_marks — when a namespace's planes last moved.

One row per (org, namespace, plane), kept at the max write time, fed by the
ClickHouse client after every INSERT into a plane table and flushed from an
in-process buffer; `last_writes` reads them. Where the server has a Keeper, a namespace's
last write is also one key of `loom.namespace_write_keys`, for read-your-writes on
any replica (`written_within`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from server.core.background import TaskSet
from server.core.ch_writes import batched_insert_settings
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
# The timer's flush in flight: one at a time.
_flush_task: asyncio.Task[int] | None = None


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
    for org, ns in seen:
        _land_key_before_answer(org, ns)


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
    global _timer, _flush_task
    _timer = None
    if _flush_task is not None and not _flush_task.done():
        return  # the flush in flight re-arms when it is done
    try:
        _flush_task = _inflight.spawn(_timed_flush(), name="loom-write-marks-flush", detached=True)
    except RuntimeError:
        return


# The timer's flush runs in a detached (empty) context; the tag names its insert
# in the query log instead of the `untagged:` frame fallback.
FLUSH_QUERY_TAG = "namespace_write_marks.flush"


async def _timed_flush() -> int:
    from server.core.ch import set_query_tag

    set_query_tag(FLUSH_QUERY_TAG)
    n = await flush_now()
    if _pending:
        _arm()  # the marks touched while this flush was in flight
    return n


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
        # Batched server-side with every other process's marks: one part per
        # busy interval, not one per flush.
        await ch.insert(TABLE, rows, settings=batched_insert_settings())
    except Exception as e:  # noqa: BLE001 — telemetry-grade write; the marks stay buffered
        log.warning("namespace_write_marks flush failed (non-fatal, %d rows): %s", len(rows), e)
        _arm()
        return 0
    for key, ts in items:
        if _pending.get(key) == ts:
            del _pending[key]
    return len(rows)


RESTART_PLANE = "restart"


async def mark_process_start(*, ch: _CHClient | None = None) -> None:
    """Move the all-orgs mark: a predecessor that crashed took its unflushed
    marks with it. Waited for; a failure is logged (the next start retries)."""
    from server.core.ch import query_tag_var

    ch = ch or _default_client()
    row = {
        "org": ORG_WIDE,
        "namespace": ORG_WIDE,
        "plane": RESTART_PLANE,
        "last_write": naive_utc_now(),
    }
    tag = query_tag_var.set("namespace_write_marks.process_start")
    try:
        await ch.insert(TABLE, [row], settings=batched_insert_settings())
    except Exception as e:  # noqa: BLE001 — a missed start mark never blocks boot
        log.warning("namespace_write_marks: process-start mark failed (non-fatal): %s", e)
    finally:
        query_tag_var.reset(tag)


KEYS_TABLE = "loom.namespace_write_keys"
KEY_QUERY_TAG = "namespace_write_marks.key"
_SEP = "\x1f"
_KEY_INSERT = f"INSERT INTO {KEYS_TABLE} (key) SETTINGS async_insert = 0 VALUES ({{key:String}})"
_KEYS_READ = (
    f"SELECT count() AS n FROM {KEYS_TABLE} WHERE key IN {{keys:Array(String)}} "
    "AND last_write >= now64(3) - toIntervalMillisecond({ms:UInt32})"
)
# How long an answer waits for its write's key; a slower key lands detached.
KEY_WAIT_S = 1.0


def _key(org: str, namespace: str) -> str:
    return f"{org}{_SEP}{namespace or ORG_WIDE}"


def _land_key_before_answer(org: str, namespace: str) -> None:
    """Set the key now, beside the rest of the request, and have the answer wait for
    it (at most `KEY_WAIT_S`): the caller's next read, on any replica, sees it
    (`written_within`) and reads with sequential consistency. Once per namespace per
    request."""
    from server.core import request_tail

    step = f"{KEY_QUERY_TAG}:{_key(org, namespace)}"
    if not request_tail.active() or request_tail.deferred(step):
        return
    task = _inflight.spawn(land_key(org, namespace), name="loom-write-key", detached=True)
    request_tail.defer_once(step, partial(asyncio.wait, [task], timeout=KEY_WAIT_S))


async def land_key(org: str, namespace: str, *, ch: _CHClient | None = None) -> None:
    """(org, namespace) was written now, by the server's clock: its key is set (it
    overwrites the last one). Best-effort."""
    from server.core.ch import query_tag_var

    tag = query_tag_var.set(KEY_QUERY_TAG)
    try:
        ch = ch or _default_client()
        await ch.execute(_KEY_INSERT, {"key": _key(org, namespace)})
    except Exception as e:  # noqa: BLE001 — a lost key only skips one sequential read
        log.warning("namespace write key not set (%s/%s): %s", org, namespace, e)
    finally:
        query_tag_var.reset(tag)


def pending() -> dict[tuple[str, str, str], datetime]:
    return dict(_pending)


def _reset_for_tests() -> None:
    global _timer, _flush_task
    _pending.clear()
    _flush_task = None
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
    recorded write since the marks began.

    This process's unflushed marks are folded in, so it reads its own writes at
    once; another process sees them after the flush (at most
    `namespace_write_marks_flush_interval_s`, against one part per write). `bump`
    records a write no plane-table INSERT carries (a mutation, such as an erasure).
    The org-wide sentinel namespace `*` marks writes whose namespace is not known at
    the call site. A process that dies loses its unflushed marks, so every writing
    process moves the all-orgs mark (`*`, `*`) when it starts (`mark_process_start`),
    and that is folded in too: no precedent proven unmoved before a lost mark is
    served after its replacement boots."""
    if not namespaces:
        return {}
    ch = ch or _default_client()
    wanted = sorted({*namespaces, ORG_WIDE})
    rows = await ch.query_all(
        f"""
        SELECT namespace, max(last_write) AS last_write
        FROM {TABLE}
        WHERE (org = {{org:String}} AND namespace IN {{nss:Array(String)}})
           OR (org = '{ORG_WIDE}' AND namespace = '{ORG_WIDE}')
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


async def written_within(
    org: str, namespace: str, window_s: float, *, ch: _CHClient | None = None
) -> bool:
    """Whether (org, namespace) or its org-wide sentinel was written within the last
    `window_s` seconds, by this process (its unflushed marks) or any process.

    The caller's next request may land on any replica. Each write sets its
    namespace's key, stamped by the server's clock, beside the request, and the
    answer waits for it (at most `KEY_WAIT_S`): one Keeper write, no part."""
    since = naive_utc_now() - timedelta(seconds=window_s)
    for (p_org, p_ns, _plane), at in _pending.items():
        if p_org == org and p_ns in (namespace, ORG_WIDE) and at >= since:
            return True
    ch = ch or _default_client()
    row = await ch.query_one(
        _KEYS_READ,
        {"keys": [_key(org, namespace), _key(org, ORG_WIDE)], "ms": int(window_s * 1000)},
    )
    return bool(row and int(row.get("n") or 0) > 0)


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
            settings=batched_insert_settings(),
        )
    except Exception as e:  # noqa: BLE001 — telemetry-grade write
        log.warning("namespace_write_marks bump failed (%s/%s/%s): %s", org, namespace, plane, e)
    await land_key(org, namespace, ch=ch)
