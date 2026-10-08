"""The `loom.events` stream: budget violations, queued in-process and written in batches.

A budget violation is a latency reading, not an audit row. It lives on `loom.events` only
and is read by `admin_repo.budget_violations` (`memory.budget_violations`, the SLO
alerts). Rows queue in-process and a drain task writes the queue as ONE committed insert
every `flush_interval_s` (or sooner at `flush_batch` rows). Identity columns are typed;
the kind-specific scalars ride a JSON payload. Telemetry never fails a request: a failed
flush is logged and counted; the loss window on a crash is the queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from server.core.background import TaskSet

log = logging.getLogger(__name__)

TABLE = "loom.events"
QUEUE_MAX = 20_000

_IDENTITY = (
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "run_id",
    "request_id",
    "actor",
    "token_id",
)
_IDENTITY_DEFAULTS: dict[str, Any] = {
    "agent": "",
    "user_id": "-",
    "session_id": "-",
    "run_id": "-",
    "request_id": "",
    "actor": "",
    "token_id": "",
}
BUDGET_VIOLATION_KIND = "budget_violation"
KINDS = frozenset({BUDGET_VIOLATION_KIND})
COMMITTED = {"async_insert": 0, "wait_for_async_insert": 1}

_queue: list[dict[str, Any]] = []
_drain_task: asyncio.Task[None] | None = None
_stopping = False
_inflight = TaskSet()
# Rows lost, by reason (unmappable | queue_full | flush_failed | shutdown) and
# kind, so a loss is countable, not only a log line.
_dropped: dict[str, dict[str, int]] = {}


def _drop(reason: str, kinds: list[str], *, warn: bool = True) -> None:
    by_kind = _dropped.setdefault(reason, {})
    for kind in kinds:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    total = sum(by_kind.values())
    # The first loss for a reason, then one line per thousand: a full queue
    # drops on every request, and a line per row would be a log storm.
    if warn and (total == len(kinds) or total // 1000 != (total - len(kinds)) // 1000):
        log.warning(
            "events stream dropped %d %s row(s) (%s); %d dropped for this reason so far",
            len(kinds),
            ",".join(sorted(set(kinds))),
            reason,
            total,
        )


def _interval_s() -> float:
    from server.settings import settings

    return float(settings.events_stream_flush_interval_s)


def _batch() -> int:
    from server.settings import settings

    return int(settings.events_stream_flush_batch)


def to_event(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    """Map a writer's row onto one loom.events row: identity columns typed (with
    their defaults), the build/process stamps, everything else in the payload.
    An unknown kind raises KeyError."""
    if kind not in KINDS:
        raise KeyError(kind)
    ev: dict[str, Any] = {"ts": row.get("ts") or datetime.now(UTC), "kind": kind}
    consumed = {"ts"}
    for col in _IDENTITY:
        if col in row and row[col] is not None:
            ev[col] = str(row[col])
            consumed.add(col)
        else:
            ev[col] = _IDENTITY_DEFAULTS.get(col, "")
    # The build, process (`instance` = hostname:pid) and launcher
    # (`instance_label`) that produced the row; the writer's own stamp wins.
    from server.observability.instance import stamps as _stamps

    for col, val in _stamps().items():
        ev[col] = str(row.get(col) or val or "")
        consumed.add(col)
    payload = {k: v for k, v in row.items() if k not in consumed}
    ev["payload"] = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return ev


def record(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    """Queue one telemetry row of `kind`; the drain writes it with whatever else
    arrives inside the flush interval. Never blocks, never raises. Returns the
    event for callers and tests ({} when the row could not be mapped)."""
    try:
        ev = to_event(kind, row)
    except Exception as e:  # noqa: BLE001 — telemetry never fails a request
        log.warning("events stream dropped an unmappable %s event: %s", kind, e)
        _drop("unmappable", [kind], warn=False)
        return {}
    if len(_queue) >= QUEUE_MAX:
        _drop("queue_full", [kind])
        return ev
    _queue.append(ev)
    _ensure_drain()
    if len(_queue) >= _batch():
        _flush_soon()
    return ev


def _ensure_drain() -> None:
    global _drain_task
    if _drain_task is not None and not _drain_task.done():
        return
    try:
        # Process-lifetime task: it must not inherit the tag, request id or
        # auth of whichever request first started it.
        from server.core.background import detached_task

        _drain_task = detached_task(_drain_loop(), name="loom-events-stream-drain")
    except RuntimeError:
        _drain_task = None  # no loop (sync caller / unit test): queued, not written


FLUSH_QUERY_TAG = "events_stream.flush"


async def _drain_loop() -> None:
    # Detached (empty) context: tag the inserts so the query log names them.
    from server.core.ch import set_query_tag

    set_query_tag(FLUSH_QUERY_TAG)
    while not _stopping:
        await asyncio.sleep(_interval_s())
        await flush_now()


async def _tagged_flush() -> int:
    from server.core.ch import set_query_tag

    set_query_tag(FLUSH_QUERY_TAG)
    return await flush_now()


def _flush_soon() -> asyncio.Task[int] | None:
    """A flush now, because the queue reached a batch. Detached like the drain
    loop: the batch holds every queued request's rows, so the request whose
    row filled it must not lend the insert its tag and request id."""
    try:
        return _inflight.spawn(_tagged_flush(), name="loom-events-stream-flush", detached=True)
    except RuntimeError:
        return None


async def flush_now() -> int:
    """Write everything queued as one committed insert. Returns rows written."""
    if not _queue:
        return 0
    rows = _queue[:]
    del _queue[: len(rows)]
    try:
        from server.core.ch import get_client

        await get_client().insert(TABLE, rows, settings=dict(COMMITTED))
        return len(rows)
    except asyncio.CancelledError:
        # A flush cut short (shutdown cancelling the drain loop mid-insert)
        # must not lose its rows: put them back for the final drain.
        _queue[:0] = rows
        raise
    except Exception as e:  # noqa: BLE001 — telemetry never fails a request
        log.warning("events stream flush failed (non-fatal, %d rows): %s", len(rows), e)
        _drop("flush_failed", [str(r.get("kind") or "") for r in rows], warn=False)
        return 0


def queued() -> int:
    return len(_queue)


def stats() -> dict[str, Any]:
    """Queue depth and rows lost per reason and kind, for
    `/v1/health?include=events`."""
    return {
        "queued": len(_queue),
        "dropped": {reason: dict(by_kind) for reason, by_kind in _dropped.items()},
    }


async def drain(timeout_s: float = 5.0) -> None:
    """Shutdown: stop the drain task, let in-flight flushes finish, write the
    rest. Each wait is bounded by `timeout_s`; rows still queued after it are
    counted as `shutdown` drops. Runs before the ClickHouse client
    disconnects (main.py)."""
    global _drain_task, _stopping
    _stopping = True
    if _drain_task is not None:
        _drain_task.cancel()
        try:
            await _drain_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown ignores how the drain loop ended
            pass
        _drain_task = None
    if _inflight:
        # A flush cut short here puts its rows back for the final one below.
        pending = list(_inflight)
        _, still = await asyncio.wait(pending, timeout=timeout_s)
        for t in still:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    try:
        await asyncio.wait_for(flush_now(), timeout=timeout_s)
    except TimeoutError:
        log.warning("events stream drain timed out with %d rows queued", len(_queue))
    if _queue:
        _drop("shutdown", [str(r.get("kind") or "") for r in _queue])
        _queue.clear()
    _stopping = False
