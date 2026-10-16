"""When a namespace's entity graph last moved, and whether a projection of it is in flight.

`facts.traverse` remembers a walk for a minute. A walk read before a graph write must not
be served after it, and a walk should not start while a projection a `memory.set` left
running is still landing. The write and the walk rarely land on the same Loom process, so
both are recorded in `loom.graph_projections` (server/memory/store/graph_projections.py),
read by every process and stamped by the ClickHouse server's clock:

- each projection in flight: recorded before the `memory.set` that starts it answers.
  Its end records the namespace's write first and then clears it, so a walk that sees
  it cleared also sees the write.
- any other graph write (a graph mutation, an insert into a graph table): recorded
  within `_FLUSH_S`, one flush at a time, the writes of a burst together.

A walk takes its stamp from the server's clock in its first read, before it reads the
graph, so no two hosts' clocks are compared. A write of this process not recorded yet
counts as a write at the walk's stamp.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

from server.core.background import TaskSet
from server.memory.store import graph_projections

log = logging.getLogger("loom.graph_write_marks")

STALE_PENDING_S = 15.0
_POLL_S = 0.1
# How soon a graph write is recorded, and how soon again after a failed flush, seconds.
_FLUSH_S = 0.02
_RETRY_S = 1.0
FLUSH_QUERY_TAG = "graph_write_marks.flush"
END_QUERY_TAG = "graph_write_marks.end"
_ends = TaskSet()
_flushes = TaskSet()

_unrecorded: set[tuple[str, str]] = set()
_recording: set[tuple[str, str]] = set()
_timer: asyncio.TimerHandle | None = None
# Projections a walk here waited out in full (their process most likely died):
# later walks do not wait on them again but count them as a write at the walk,
# so no walk is served or remembered while they are recorded. Token -> monotonic
# expiry.
_waited_out: dict[str, float] = {}
# Namespaces with a tracked projection running in this process, and how many: the
# projection's end records the write, so its inserts are not recorded one by one.
_projecting: dict[tuple[str, str], int] = {}


def note_write(org: str, namespace: str) -> None:
    """A graph write to (org, namespace) landed now. Never raises."""
    if org and namespace and (org, namespace) not in _projecting:
        _unrecorded.add((org, namespace))
        _arm(_FLUSH_S)


def _arm(delay_s: float) -> None:
    global _timer
    if _timer is not None or _recording:
        return  # the flush in flight arms the next one
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (sync caller): recorded by an explicit flush_now()
    _timer = loop.call_later(delay_s, _fire)


def _fire() -> None:
    global _timer
    _timer = None
    try:
        _flushes.spawn(_timed_flush(), name="loom-graph-marks-flush", detached=True)
    except RuntimeError:
        return


async def _timed_flush() -> None:
    from server.core.ch import set_query_tag

    set_query_tag(FLUSH_QUERY_TAG)
    await flush_now()


async def flush_now() -> bool:
    """Record the writes noted so far; False when that failed (they are retried)
    or another flush is in flight."""
    if _recording:
        return False
    _recording.update(_unrecorded)
    _unrecorded.clear()
    ok = True
    if _recording:
        try:
            await graph_projections.note_writes(sorted(_recording))
        except Exception as e:  # noqa: BLE001 — the writes stay unrecorded and are retried
            log.warning("graph writes not recorded (non-fatal, %d): %s", len(_recording), e)
            _unrecorded.update(_recording)
            ok = False
        _recording.clear()
    if _unrecorded:
        _arm(_FLUSH_S if ok else _RETRY_S)
    return ok


def track(
    org: str, namespace: str, projection: asyncio.Task[Any] | None
) -> Coroutine[Any, Any, None]:
    """The coroutine a write awaits before it answers: it records `projection` in
    flight for every process, and ends the record once `projection` is done (at
    once without one). Never raises."""
    if projection is not None:
        key = (org, namespace)
        _projecting[key] = _projecting.get(key, 0) + 1
        # Registered before the end's callback, so the end's write is recorded.
        projection.add_done_callback(lambda _t: _projected(key))

    async def _begin() -> None:
        begun = time.monotonic()
        token, certain = None, True
        try:
            token = await graph_projections.begin(org, namespace, stale_s=STALE_PENDING_S)
        except graph_projections.BeginUncertain as e:
            token, certain = e.token, False  # it may have committed: ended all the same
            log.warning("graph projection begin uncertain (non-fatal): %s", e)
        except Exception as e:  # noqa: BLE001 — the write never fails on its in-flight mark
            log.warning("graph projection mark not written (non-fatal): %s", e)
        if token is None:
            # No walk elsewhere waits on it: it counts as a write now, and again at its end.
            await _record_write(org, namespace)
        if projection is None:
            end_pending(org, namespace, token, begun, certain)
        else:
            projection.add_done_callback(
                lambda _t: end_pending(org, namespace, token, begun, certain)
            )

    return _begin()


def _projected(key: tuple[str, str]) -> None:
    left = _projecting.pop(key, 1) - 1
    if left:
        _projecting[key] = left


async def _record_write(org: str, namespace: str) -> None:
    try:
        await graph_projections.note_writes([(org, namespace)])
    except Exception as e:  # noqa: BLE001 — retried by the flush
        log.warning("graph write not recorded (non-fatal): %s", e)
        note_write(org, namespace)


def end_pending(
    org: str, namespace: str, token: str | None, begun: float, certain: bool = True
) -> None:
    if token is None:
        note_write(org, namespace)
        return
    try:
        _ends.spawn(
            _end(org, namespace, token, begun, certain), name="loom-graph-end", detached=True
        )
    except RuntimeError:
        note_write(org, namespace)  # no loop: the record goes stale after STALE_PENDING_S


async def _end(org: str, namespace: str, token: str, begun: float, certain: bool) -> None:
    from server.core.ch import set_query_tag

    set_query_tag(END_QUERY_TAG)  # a detached task: its own context
    try:
        # The write first: a walk that sees the projection ended sees its write.
        await graph_projections.note_writes([(org, namespace)])
        await graph_projections.end(
            org,
            namespace,
            token,
            held_s=time.monotonic() - begun,
            stale_s=STALE_PENDING_S,
            certain=certain,
        )
    except Exception as e:  # noqa: BLE001 — detached; the record goes stale after STALE_PENDING_S
        log.warning("graph projection end not written (non-fatal): %s", e)
        note_write(org, namespace)  # the flush retries the write


async def drain(timeout_s: float) -> int:
    """Shutdown: the ends and writes still being recorded land (at most
    `timeout_s`), so no walk on another process waits on a projection this one
    finished."""
    await asyncio.sleep(0)  # the done callbacks of a projection just drained spawn its end
    live = [t for t in (*_ends, *_flushes) if not t.done()]
    if _unrecorded:
        live.append(asyncio.ensure_future(flush_now()))
    if live:
        await asyncio.wait(live, timeout=timeout_s)
    return len(live)


async def last_write(
    org: str, namespace: str, *, wait_s: float
) -> tuple[datetime | None, datetime]:
    """(the last graph write to (org, namespace) any process recorded or None, the
    server's clock when this was called), read after waiting at most `wait_s` for
    the projections of it in flight in any process when this is called. One still
    in flight after the wait, or a write of this process not recorded yet, counts
    as a write at that time."""
    deadline = time.monotonic() + wait_s
    own = (org, namespace) in _unrecorded or (org, namespace) in _recording
    snap = await graph_projections.snapshot(org, namespace, stale_s=STALE_PENDING_S)
    at = snap.now
    now = time.monotonic()
    if any(_waited_out.get(t, 0.0) > now for t in snap.busy):
        return at, at  # waited out once already: not waited on again, still a write
    # Only the projections that began before this walk: later ones are after it.
    busy = set(snap.busy)
    while busy and (left := deadline - time.monotonic()) > 0:
        await asyncio.sleep(min(_POLL_S, left))
        snap = await graph_projections.snapshot(org, namespace, stale_s=STALE_PENDING_S)
        busy &= snap.busy
    if busy:
        _remember_waited_out(busy)
        return at, at
    if own:
        return max(at, snap.written or at), at
    return snap.written, at


def _remember_waited_out(tokens: set[str]) -> None:
    now = time.monotonic()
    if len(_waited_out) > 1024:
        for t in [t for t, until in _waited_out.items() if until <= now]:
            del _waited_out[t]
    for t in tokens:
        _waited_out[t] = now + STALE_PENDING_S


def _reset_for_tests() -> None:
    global _timer
    _unrecorded.clear()
    _recording.clear()
    _waited_out.clear()
    _projecting.clear()
    if _timer is not None:
        _timer.cancel()
        _timer = None


__all__ = [
    "STALE_PENDING_S",
    "drain",
    "end_pending",
    "flush_now",
    "last_write",
    "note_write",
    "track",
]
