"""The current worker pass, for legs that fail without failing the pass.

A worker that catches a leg's exception, logs at WARNING and carries on is
right to carry on — the embedding worker's orphan reconciliation must never
cost the stubs their embeddings — but the supervisor then records a clean
pass, and a leg can fail for hours while the pass reports only successes.

`note_failure(exc, what)` at such a site records the failure on the pass
the supervisor bound (a ContextVar, so it is scoped to the supervised task);
after the pass the supervisor counts a pass with noted failures toward the
worker's failure streak, publishes `loom.worker.partial_failures{worker}`
and writes the failures onto the pass's ledger row. Outside a supervised
pass (a request awaiting `run_pass`, a test) the call is a debug line.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# A pass keeps this many noted failures verbatim; the count is unbounded.
MAX_KEPT = 8
# A resume cursor is a key or a timestamp, not a payload.
MAX_CURSOR = 512
# A namespace label (LowCardinality(String) on the row).
MAX_NAMESPACE = 256


@dataclass
class PassContext:
    worker: str
    count: int = 0
    # (what, error class, bounded first line) for the first MAX_KEPT failures.
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    # The cursor the previous pass left in `loom.worker_status`
    # (read by the supervisor at loop start, across colours) and the one this
    # pass leaves. A worker that pages through work reads `resume_cursor()`
    # and calls `set_cursor()` where it stopped; the supervisor writes it on
    # the pass row. Empty when the worker has no notion of a cursor.
    resume_cursor: str = ""
    cursor: str = ""
    # The last namespace this pass
    # reached (`set_namespace`), written on the row so a pass cut at its
    # deadline says WHERE it stopped — the code workers' legs and the
    # graph-stats resume point are per namespace.
    namespace: str = ""
    # The pass found nothing to do. A worker whose spec names
    # an `idle_interval_seconds` is next run after that instead of its
    # interval: it asks less often while the last answer was "nothing".
    idle: bool = False
    # The outcome `status.record_pass(worker)` recorded when this pass's
    # block finished cleanly; None when it recorded none (it raised, it was
    # cancelled, or the worker does not use `record_pass`). The status row
    # reads it here, never from the registry, whose outcome may be an
    # earlier pass's.
    outcome: dict[str, Any] | None = None
    # Set when the supervisor stopped this pass (cancelled, or cut at its
    # deadline): code of the pass still running, in a thread or past a
    # swallowed cancel, sees it at its next `check_stop()`.
    stopped: threading.Event = field(default_factory=threading.Event)

    def note_failure(self, exc: BaseException, what: str = "") -> None:
        self.count += 1
        if len(self.failures) >= MAX_KEPT:
            return
        try:
            from server.observability.event_args import bounded_error

            cls, msg = bounded_error(exc)
        except Exception:  # noqa: BLE001 — the note goes in without the text
            cls, msg = type(exc).__name__, ""
        self.failures.append((str(what or "")[:64], cls, msg))

    def first(self) -> tuple[str, str, str] | None:
        return self.failures[0] if self.failures else None


_current: contextvars.ContextVar[PassContext | None] = contextvars.ContextVar(
    "loom_worker_pass_context", default=None
)


def begin(worker: str, *, resume_cursor: str = "") -> PassContext:
    """Bind a fresh context for `worker`'s pass to the current task."""
    ctx = PassContext(worker=worker, resume_cursor=str(resume_cursor or "")[:MAX_CURSOR])
    _current.set(ctx)
    return ctx


class PassStopped(BaseException):
    """The pass was stopped, or the supervisor lease is no longer fresh: no
    further write. A BaseException, like a cancel, so a leg's `except
    Exception` does not carry on past it."""


def stop_requested() -> bool:
    """Inside a supervised pass: the supervisor stopped it, or this process's
    lease has gone stale (a pause, a stalled network) and another may hold it
    by now. Thread-safe, no ClickHouse call. False outside a pass."""
    ctx = _current.get()
    if ctx is None:
        return False
    if ctx.stopped.is_set():
        return True
    from server.workers import leader

    lease = leader.current()
    return lease is not None and not lease.fresh()


def check_stop() -> None:
    """Raise `PassStopped` when `stop_requested()`. Called between batches of a
    long pass, in its threads too (`asyncio.to_thread` carries the context)."""
    if stop_requested():
        raise PassStopped


def current() -> PassContext | None:
    return _current.get()


def clear() -> None:
    _current.set(None)


def note_failure(exc: BaseException, what: str = "") -> None:
    """Record a leg failure on the current pass. Never raises."""
    try:
        ctx = _current.get()
        if ctx is None:
            log.debug("pass_context.note_failure outside a supervised pass: %s (%s)", what, exc)
            return
        ctx.note_failure(exc, what)
    except Exception:  # noqa: BLE001 — a pass hook never raises into the pass
        pass


def record_outcome(worker: str, outcome: dict[str, Any]) -> None:
    """Keep `outcome` as this pass's when `worker` is the pass's worker.
    Never raises; outside a supervised pass it does nothing."""
    try:
        ctx = _current.get()
        if ctx is not None and ctx.worker == worker:
            ctx.outcome = dict(outcome)
    except Exception:  # noqa: BLE001 — a pass hook never raises into the pass
        pass


def resume_cursor() -> str:
    """The cursor the last pass of this worker left in the store ('' when
    none, or outside a supervised pass). A restarted pass —
    a new colour, a new process — reads where the previous one stopped."""
    ctx = _current.get()
    return ctx.resume_cursor if ctx is not None else ""


def set_cursor(value: str) -> None:
    """Leave `value` for the next pass to read via `resume_cursor()`. The
    supervisor writes it on this pass's `loom.worker_status` row. Never
    raises; a call outside a supervised pass is a debug line."""
    try:
        ctx = _current.get()
        if ctx is None:
            log.debug("pass_context.set_cursor outside a supervised pass: %r", value)
            return
        ctx.cursor = str(value or "")[:MAX_CURSOR]
    except Exception:  # noqa: BLE001 — a pass hook never raises into the pass
        pass


def mark_idle() -> None:
    """Say this pass found no work: the supervisor sleeps the
    spec's `idle_interval_seconds` (when set) instead of its interval. Never
    raises; a call outside a supervised pass is a debug line."""
    try:
        ctx = _current.get()
        if ctx is None:
            log.debug("pass_context.mark_idle outside a supervised pass")
            return
        ctx.idle = True
    except Exception:  # noqa: BLE001 — a pass hook never raises into the pass
        pass


def set_namespace(value: str) -> None:
    """Record `value` as the namespace this pass has reached; the supervisor writes the last
    one on this pass's `loom.worker_status` row. Never raises; a call outside a supervised
    pass is a debug line."""
    try:
        ctx = _current.get()
        if ctx is None:
            log.debug("pass_context.set_namespace outside a supervised pass: %r", value)
            return
        ctx.namespace = str(value or "")[:MAX_NAMESPACE]
    except Exception:  # noqa: BLE001 — a pass hook never raises into the pass
        pass
