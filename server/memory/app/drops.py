"""Memory ingest drop ledger.

Every point where the ingest pipeline discards, truncates or gives up on a
piece of data calls `record_drop(...)`. Drops are batched in-process and
flushed to `loom.memory_drops` (30-day TTL) and mirrored to the OTel
counter `loom.memory.drops{stage,reason}` so "how much did we lose, where"
is answerable from a table and a dashboard instead of from log archaeology.

Contract:
  * `record_drop` NEVER raises and never blocks the caller. It is safe to
    call from sync helpers and from any async context; a failure to
    schedule or flush is logged at debug and the drop is kept in the buffer
    for the next flush (or discarded when the buffer is over its cap).
  * `write_drops` is the exception, for a caller whose drop must not be
    lost: it inserts synchronously and raises, so the caller keeps the item.
  * Stage and reason are bounded enums by convention — they land in
    LowCardinality columns and OTel attributes. Free text goes in `item`
    (a memory key, field name, ...), which is NEVER used as a metric label.

Stages wired: extract.parse, extract.judge, extract.truncate, write,
entity.extract, consolidate.merge, records.field, chunk.split, learnings.file, and the
`op` of a written 'high' row whose fence failed or ran with its commit's outcome unknown
(`high_fence`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from server.core.datetimes import naive_utc_now

log = logging.getLogger(__name__)

TABLE = "loom.memory_drops"

# Flush when this many rows are buffered, or _FLUSH_DELAY_S after the first
# buffered row, whichever comes first.
_FLUSH_BATCH = 64
_FLUSH_DELAY_S = 2.0
# Hard cap on buffered rows so a flush outage can't grow memory without bound;
# the oldest rows are dropped past it (and that loss is logged at debug).
_BUFFER_CAP = 10_000
_ITEM_MAX_LEN = 256

_buffer: list[dict[str, Any]] = []
_lock = threading.Lock()
_flush_scheduled = False
_timer: asyncio.TimerHandle | None = None


def _row(org: str, namespace: str, stage: str, reason: str, item: str, n: int) -> dict[str, Any]:
    return {
        "org": str(org or ""),
        "namespace": str(namespace or ""),
        "stage": str(stage or "")[:64],
        "reason": str(reason or "")[:64],
        "item": str(item or "")[:_ITEM_MAX_LEN],
        "n": int(n),
        "ts": naive_utc_now(),
    }


def _count(row: dict[str, Any]) -> None:
    try:
        from server.observability.metrics import record_memory_drop

        record_memory_drop(
            namespace=row["namespace"], stage=row["stage"], reason=row["reason"], n=row["n"]
        )
    except Exception as e:  # noqa: BLE001 — the metric must not lose the drop row
        log.debug("drops: metric emit failed: %s", e)


def record_drop(
    org: str,
    namespace: str,
    stage: str,
    reason: str,
    *,
    item: str = "",
    n: int = 1,
) -> None:
    """Record that `n` items were dropped at `stage` for `reason`.

    Fail-open: never raises. Emits the OTel counter immediately and buffers
    the table row for the next flush.
    """
    global _flush_scheduled
    try:
        if n <= 0:
            return
        row = _row(org, namespace, stage, reason, item, n)
        _count(row)
        with _lock:
            if len(_buffer) >= _BUFFER_CAP:
                log.debug("drops: buffer over cap (%d); discarding oldest row", _BUFFER_CAP)
                del _buffer[0]
            _buffer.append(row)
            size = len(_buffer)
            need_schedule = not _flush_scheduled
            if need_schedule:
                _flush_scheduled = True
        if need_schedule and not _flush_later(0.0 if size >= _FLUSH_BATCH else _FLUSH_DELAY_S):
            # No running loop (sync CLI / test helper): stay buffered; the
            # next call from an async context, or flush_drops(), writes it.
            with _lock:
                _flush_scheduled = False
    except Exception as e:  # noqa: BLE001 — the ledger must never hurt ingest
        log.debug("drops: record_drop failed: %s", e)


def _flush_later(delay: float) -> bool:
    """Arm a one-shot timer that flushes after `delay` seconds.

    A timer handle rather than a sleeping task on purpose: a pending task
    left behind when the loop closes (request-scoped test loops, CLI exits)
    logs "Task was destroyed but it is pending"; an unfired timer handle is
    dropped silently and the rows stay buffered for flush_drops(). Returns
    False when there is no running loop to arm.
    """
    global _timer
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _timer = loop.call_later(delay, _fire_flush)
    return True


def _fire_flush() -> None:
    global _flush_scheduled, _timer
    _timer = None
    try:
        from server.core.background import detached_task, register_deferred_task

        # The flush serves every request's drops; it carries none of them.
        # `call_later` runs this in the arming request's context, which
        # create_task would otherwise copy into the flush.
        register_deferred_task(detached_task(_flush_and_rearm(), name="loom-drops-flush"))
    except RuntimeError:
        with _lock:
            _flush_scheduled = False


async def _flush_and_rearm() -> None:
    global _flush_scheduled
    from server.core.ch import query_tag

    try:
        with query_tag("drops.flush"):
            await flush_drops()
    except Exception as e:  # noqa: BLE001 — the timer re-arms whatever the flush did
        log.debug("drops: scheduled flush failed: %s", e)
    finally:
        with _lock:
            _flush_scheduled = False
        # Rows recorded during the flush: arm another pass.
        if pending_count():
            with _lock:
                _flush_scheduled = True
            if not _flush_later(_FLUSH_DELAY_S):
                with _lock:
                    _flush_scheduled = False


async def write_drops(
    items: list[tuple[str, str, str]], stage: str, reason: str, *, ch: Any | None = None
) -> None:
    """Write one row per `(org, namespace, item)` now, synchronously; raises
    when ClickHouse does not take them, so the caller can keep the items."""
    from server.core.ch import get_client

    rows = [_row(org, ns, stage, reason, item, 1) for org, ns, item in items]
    if not rows:
        return
    await (ch or get_client()).insert(
        TABLE, rows, settings={"async_insert": 0, "wait_for_async_insert": 1}
    )
    for row in rows:
        _count(row)


async def flush_drops() -> int:
    """Write every buffered row to loom.memory_drops. Returns rows written.

    Fail-open: on a ClickHouse error the rows go back to the front of the
    buffer (bounded by _BUFFER_CAP) and the error is logged at debug.
    Call from the server lifespan on shutdown and from tests.
    """
    with _lock:
        rows = list(_buffer)
        _buffer.clear()
    if not rows:
        return 0
    try:
        from server.core.ch import get_client

        await get_client().insert(
            TABLE, rows, settings={"async_insert": 1, "wait_for_async_insert": 0}
        )
        return len(rows)
    except Exception as e:  # noqa: BLE001 — fail open: rows go back into the bounded buffer
        log.debug("drops: flush of %d row(s) failed (%s); re-buffering", len(rows), e)
        with _lock:
            _buffer[:0] = rows
            del _buffer[_BUFFER_CAP:]
        return 0


def pending_count() -> int:
    with _lock:
        return len(_buffer)


def _peek_for_tests() -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _buffer]


def _reset_for_tests() -> None:
    global _flush_scheduled, _timer
    with _lock:
        _buffer.clear()
        _flush_scheduled = False
    if _timer is not None:
        _timer.cancel()
        _timer = None


__all__ = ["TABLE", "flush_drops", "pending_count", "record_drop"]
