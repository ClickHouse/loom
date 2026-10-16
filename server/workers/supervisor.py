"""WorkerSupervisor — restart-with-backoff for background loops.

A crashed worker stays alive across pass failures instead of dying
silently:

  - Each supervised worker runs in a wrapper that catches exceptions,
    records the failure in `WorkerStatusRegistry`, and sleeps for an
    exponentially-backed-off interval before retrying its pass.
  - After `alert_threshold` consecutive failures, the supervisor logs
    an ERROR and marks the worker `degraded` in the status registry —
    operators see it on `/v1/admin/workers.status` immediately, not
    after the K8s liveness probe finally restarts the pod.
  - Graceful shutdown drains the supervisor: each task is cancelled,
    given a chance to finish its current pass, then awaited with a
    bounded timeout before exit.

The pass function (`WorkerSpec.pass_callable`) is the only thing the
worker module has to provide. The supervisor owns the loop, the
backoff, the failure tracking, and the lifecycle.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import zlib
from dataclasses import dataclass
from typing import Any

from server.core.model_work_gate import get_model_work_gate
from server.observability.traffic_class import bind_background
from server.workers import leader, pass_context, status_store
from server.workers.registry import (
    FIRST_PASS_DUE_TIMEOUT_SECONDS,
    WorkerSpec,
    enabled_workers,
)
from server.workers.status import get_registry, set_worker_identity

log = logging.getLogger(__name__)


# Exponential backoff: 1s, 2s, 4s, ... capped at 60s.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 60.0
_ALERT_THRESHOLD = 3
# A pass with a failed leg on a worker whose interval is longer than this
# retries after this long, doubling with each further such pass, up to the
# interval: a weekly pass must not leave one namespace's transient failure
# standing for a week.
_PARTIAL_RETRY_SECONDS = 3600.0


def _record_cancelled(spec: WorkerSpec, started: float, ctx: Any, state: Any, streak: int) -> None:
    """The status row of a pass stopped before it finished."""
    _record_status_row(
        spec.name,
        started=started,
        success=False,
        partial=ctx,
        spec=spec,
        state=state,
        streak=streak,
        cancelled=True,
    )


def _report_failure(
    spec: WorkerSpec, exc: Exception, ctx: Any, started: float, deadline: float, streak: int
) -> None:
    """Publish and log a failed pass: the streak and partial gauges, the error, and
    the DEGRADED line once the streak reaches the alert threshold."""
    # The DEGRADED line below is a log; this is the alertable form.
    _publish_streak(spec.name, streak)
    _publish_partial(spec.name, ctx.count)
    duration_ms = int((time.monotonic() - started) * 1000)
    if isinstance(exc, TimeoutError):
        log.error(
            "%s: pass exceeded its deadline of %.0fs and was cancelled "
            "(consecutive_failures=%d, duration_ms=%d)",
            spec.name,
            deadline,
            streak,
            duration_ms,
        )
    else:
        log.exception(
            "%s: pass failed (consecutive_failures=%d, duration_ms=%d)",
            spec.name,
            streak,
            duration_ms,
        )
    if streak >= _ALERT_THRESHOLD:
        log.error(
            "%s: DEGRADED — %d consecutive failures. Check upstream "
            "(ClickHouse, Langfuse, external API) and operator dashboard.",
            spec.name,
            streak,
        )


class WorkerSupervisor:
    """Spawns one supervised task per worker spec; manages shutdown."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()
        self._workers: list[WorkerSpec] = []

    def start_all(self) -> None:
        """Boot all enabled workers. Idempotent: a worker whose task still runs
        (one that did not drain when a lost lease stopped them) is not started
        a second time; it starts again once that task ends."""
        self._tasks = [t for t in self._tasks if not t.done()]
        running = {t.get_name(): t for t in self._tasks}
        if running:
            log.warning(
                "WorkerSupervisor: %d workers still running; each restarts when it ends",
                len(running),
            )
        # A supervisor stopped by a lost lease starts again when it is regained.
        self._stopping.clear()

        self._workers = enabled_workers()
        if not self._workers:
            log.info("WorkerSupervisor: no workers enabled")
            return

        log.info(
            "WorkerSupervisor: starting %d workers — %s",
            len(self._workers),
            [w.name for w in self._workers],
        )
        for spec in self._workers:
            old = running.get(_task_name(spec))
            if old is not None:
                old.add_done_callback(functools.partial(self._restart, spec))
                continue
            self._spawn(spec)

    def _spawn(self, spec: WorkerSpec) -> None:
        # Seed the gauges at 0 for every supervised worker so "no series"
        # means "not supervised", never "has not failed yet".
        _publish_streak(spec.name, 0)
        _publish_partial(spec.name, 0)
        task = asyncio.create_task(self._supervise_as_background(spec), name=_task_name(spec))
        self._tasks.append(task)

    def _restart(self, spec: WorkerSpec, _ended: asyncio.Task[None]) -> None:
        """A worker that was still cancelling when the supervisor started
        again: start it now, unless the supervisor stopped meanwhile."""
        if self._stopping.is_set():
            return
        self._tasks = [t for t in self._tasks if not t.done()]
        if any(t.get_name() == _task_name(spec) for t in self._tasks):
            return
        log.info("WorkerSupervisor: %s drained late; starting it", spec.name)
        self._spawn(spec)

    async def stop_all(self, drain_timeout_s: float = 10.0) -> None:
        """Cancel every supervised task and wait (bounded) for graceful exit.

        After cancellation, each worker's `asyncio.CancelledError` should
        bubble through the inner try/except so the supervised loop exits
        cleanly. drain_timeout_s caps how long we wait — beyond that,
        we log a warning but return so the lifespan exit can proceed.
        """
        if not self._tasks:
            return
        self._stopping.set()
        log.info("WorkerSupervisor: cancelling %d workers", len(self._tasks))
        for t in self._tasks:
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=drain_timeout_s,
            )
        except TimeoutError:
            still_running = [t.get_name() for t in self._tasks if not t.done()]
            log.warning(
                "WorkerSupervisor: %d tasks did not drain within %ss — %s",
                len(still_running),
                drain_timeout_s,
                still_running,
            )
        # The last pass of a stopping colour reaches the store.
        await status_store.drain()
        # One that did not drain stays tracked: a restart must not run it twice.
        self._tasks = [t for t in self._tasks if not t.done()]

    def status(self) -> list[dict[str, object]]:
        """Snapshot for `/v1/admin/workers.status`. Reads from the existing
        WorkerStatusRegistry to avoid duplicating state."""
        reg = get_registry()
        snap = reg.snapshot()
        return [
            {
                "name": spec.name,
                "interval_seconds": spec.interval_seconds,
                "max_pass_seconds": spec.deadline_seconds(),
                # Only the specs that have one; every other
                # worker's snapshot is unchanged.
                **(
                    {"idle_interval_seconds": spec.idle_interval_seconds}
                    if spec.idle_interval_seconds
                    else {}
                ),
                "task_alive": any(
                    t.get_name().endswith(f":{spec.name}") and not t.done() for t in self._tasks
                ),
                **snap.get(spec.name, {}),
            }
            for spec in self._workers
        ]

    # ------------------------------------------------------------------

    async def _supervise_as_background(self, spec: WorkerSpec) -> None:
        """The worker's task runs as `background` —
        every read a pass issues carries the class's ClickHouse settings
        (lower priority, capped threads, cache-bypass), never the unclassified
        base bucket, which competes with production reads on equal terms. Bound
        here, in the task, so a direct `_supervise` call (a test) binds nothing."""
        bind_background(f"worker:{spec.name}")
        await self._supervise(spec)

    async def _supervise(self, spec: WorkerSpec) -> None:
        """One supervised worker loop: run the pass, sleep, repeat. On failure,
        sleep a backed-off duration and reset the backoff on the next success."""
        consecutive_failures = 0
        # Each supervised loop is its own task, so this ContextVar write is
        # scoped to the worker: every llm.*/embed.* span the pass creates
        # carries loom.operation=worker.<name> (per-repo code adds org/ns).
        set_worker_identity(spec.name)
        deadline = spec.deadline_seconds()
        stagger, state = await _begin_loop(spec)
        if stagger > 0:
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=stagger)
            except TimeoutError:
                pass

        while not self._stopping.is_set():
            if not _lease_fresh():
                # A holder that missed its renewals (a pause, a stalled
                # network) starts nothing until it renews: another process
                # may hold the lease by now. Its lease loop stops or resumes us.
                await self._sleep_or_drain(_STALE_LEASE_RECHECK_SECONDS)
                continue
            started = time.monotonic()
            # A pass is a paid operation like a request. The
            # accumulator bound here is what the pass's LLM / embedder calls
            # report into; it is drained into one ledger row afterwards.
            acc = _begin_pass_accounting()
            # Legs that fail and continue note it here.
            ctx = pass_context.begin(spec.name, resume_cursor=state.cursor)
            # True only while the pass itself is awaited, so a
            # stop that lands in the interval sleep writes no `cancelled` row
            # — a row is what a pass did, and no pass was running.
            in_pass = False
            try:
                # A pass past its deadline is cancelled and counted as a
                # failure (TimeoutError).
                async with get_model_work_gate().admit(wait=True):
                    # Maintenance waiting consumes neither the pass deadline
                    # nor its duration/accounting. An admitted pass finishes
                    # normally; changing models never cancels it mid-write.
                    started = time.monotonic()
                    in_pass = True
                    async with asyncio.timeout(deadline):
                        await spec.pass_callable()
                in_pass = False
                consecutive_failures = _after_pass(
                    spec, acc, ctx, state, started=started, streak=consecutive_failures
                )
                # A clean pass that found nothing sleeps the
                # spec's idle interval; a pass that did work keeps the
                # interval; one that noted a failed leg retries sooner on a
                # long-interval worker.
                await self._sleep_or_drain(
                    next_interval_seconds(
                        spec, ctx, partial=bool(ctx.count), streak=consecutive_failures
                    )
                )
            except pass_context.PassStopped:
                # The pass saw a stop or a stale lease between batches. Recorded
                # like a cancel; the loop top waits until the lease is fresh.
                log.warning("%s: pass stopped (lease not fresh); no further writes", spec.name)
                _record_cancelled(spec, started, ctx, state, consecutive_failures)
            except asyncio.CancelledError:
                log.info("%s: cancelled, exiting supervised loop", spec.name)
                # The store says the colour stopped mid-pass.
                # Only when it did — a stop during the interval sleep would
                # write a `cancelled` row that repeats the previous pass's
                # counts with the sleep as its duration.
                if in_pass:
                    _record_cancelled(spec, started, ctx, state, consecutive_failures)
                raise
            except Exception as exc:  # noqa: BLE001 — a failed pass must not end the loop
                consecutive_failures += 1
                _finish_pass(
                    spec.name,
                    acc,
                    started=started,
                    success=False,
                    error_code=type(exc).__name__,
                    error=exc,
                    partial=ctx,
                    spec=spec,
                    state=state,
                    streak=consecutive_failures,
                    deadline=deadline if isinstance(exc, TimeoutError) else 0.0,
                )
                _report_failure(spec, exc, ctx, started, deadline, consecutive_failures)
                backoff = min(
                    _BACKOFF_CAP_SECONDS,
                    _BACKOFF_BASE_SECONDS * (2 ** (consecutive_failures - 1)),
                )
                # A pass cut at its deadline, or any failed pass
                # of a worker whose interval is longer than its deadline, waits
                # the interval — a 1 s backoff would re-run a weekly pass from
                # its first namespace every few minutes.
                await self._sleep_or_drain(failure_hold_seconds(spec, exc, backoff))
            finally:
                # Work of this pass still running (a thread, a cut at the
                # deadline) stops at its next check_stop().
                ctx.stopped.set()
                pass_context.clear()

    async def _sleep_or_drain(self, seconds: float) -> None:
        """Sleep but wake immediately when stop_all() is called."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            pass  # normal — interval elapsed without a stop signal


def _task_name(spec: WorkerSpec) -> str:
    return f"loom-worker:{spec.name}"


_STALE_LEASE_RECHECK_SECONDS = 1.0


def _lease_fresh() -> bool:
    """The supervisor lease was renewed recently enough to start a pass
    (always, outside `auto`, or with no lease set)."""
    lease = leader.current()
    return lease is None or lease.fresh()


async def first_pass_is_due(spec: WorkerSpec) -> bool:
    """The worker's own answer to "is your output missing or stale?", bounded by
    FIRST_PASS_DUE_TIMEOUT_SECONDS and fail-quiet — an error or a slow ClickHouse reads as
    "not due", the default behaviour (stagger, then the interval). No hook → not due. A spec
    with owned tables and no hook of its own derives one — due when an owned table holds no
    row for a namespace in scope."""
    hook = spec.first_pass_hook()
    if hook is None:
        return False
    try:
        async with asyncio.timeout(FIRST_PASS_DUE_TIMEOUT_SECONDS):
            return bool(await hook())
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — the check never blocks the loop
        log.warning(
            "%s: first_pass_due failed (%s: %s); keeping the stagger",
            spec.name,
            type(e).__name__,
            e,
        )
        return False


def next_interval_seconds(
    spec: WorkerSpec,
    ctx: pass_context.PassContext | None,
    *,
    partial: bool = False,
    streak: int = 1,
) -> float:
    """How long a finished pass holds the loop. A clean pass
    that marked itself idle (`pass_context.mark_idle()`: no stub, no orphan,
    no unentitied row) on a spec with an `idle_interval_seconds` waits that
    long. A pass with a noted failed leg on a worker whose interval is longer
    than `_PARTIAL_RETRY_SECONDS` retries after that long, doubled for each
    further such pass in a row (`streak`), never past the interval. Every other
    pass waits the interval, so the poll follows the work instead of running
    hundreds of empty passes an hour."""
    idle = spec.idle_interval_seconds
    if idle is not None and idle > 0 and not partial and ctx is not None and ctx.idle:
        return float(idle)
    interval = float(spec.interval_seconds)
    if partial and interval > _PARTIAL_RETRY_SECONDS:
        return min(interval, _PARTIAL_RETRY_SECONDS * float(2 ** max(streak - 1, 0)))
    return interval


def failure_hold_seconds(spec: WorkerSpec, exc: BaseException, backoff: float) -> float:
    """How long a failed pass holds the loop. A pass cut at
    its deadline (TimeoutError) has just spent `deadline` seconds of the
    database and would spend them again at once; a worker whose interval is
    longer than its deadline (the weekly and daily passes) has nothing a
    quick retry can fix that the next boot's first-pass check or the
    next interval would not. Both wait the interval, never less than the
    backoff. Everything else — a sub-minute worker, a transient error on a
    frequent pass — keeps the exponential backoff (1, 2, 4 … ≤
    60 s), which is what "retry soon" was written for."""
    if isinstance(exc, TimeoutError) or spec.interval_seconds > spec.deadline_seconds():
        return max(float(backoff), float(spec.interval_seconds))
    return float(backoff)


@dataclass
class LoopState:
    """What one supervised loop carries between passes for the store row:
    why the next pass runs (`trigger`) and the cursor the
    last pass left (`cursor`, read from the store once at loop start so a
    new colour continues where the old one stopped)."""

    trigger: str = "boot"
    cursor: str = ""


async def _begin_loop(spec: WorkerSpec) -> tuple[float, LoopState]:
    """The boot stagger and the loop's initial state.

    Stagger the first pass by a stable hash of the name so the
    workers do not all query ClickHouse in the boot tick. A worker whose
    output is missing runs now — the stagger spreads a boot burst, it must not
    hold a table empty. The resume cursor
    is read here, once, fail-quiet ('' = start from the beginning)."""
    stagger = first_pass_delay(spec)
    state = LoopState()
    if stagger > 0 and await first_pass_is_due(spec):
        log.info(
            "%s: first pass is due — running now instead of after the %.0fs boot stagger",
            spec.name,
            stagger,
        )
        stagger = 0.0
        state.trigger = "first_pass"
    elif stagger > 0:
        overdue = await _overdue_delay(spec, stagger)
        if overdue is not None:
            stagger, state.trigger = overdue, "overdue"
    state.cursor = await status_store.resume_cursor(spec.name)
    return stagger, state


# A worker whose last pass (in this deployment family) is older than its interval is
# overdue; it starts within this spread of boot instead of after its crc32 stagger, so
# frequent restarts cannot keep resetting a long stagger (a daily worker could otherwise go
# days without a pass). The spread keeps a boot's overdue workers from starting in one tick.
OVERDUE_SPREAD_SECONDS = 300.0


async def _overdue_delay(spec: WorkerSpec, stagger: float) -> float | None:
    """The shortened first delay for an overdue worker, None when it is not
    overdue or its last pass is unknown (fail-quiet: the crc32 stagger holds)."""
    from datetime import UTC, datetime

    from server.observability.instance import instance_label

    family = status_store.label_family(instance_label())
    last = await status_store.last_pass_finished(spec.name, family)
    if last is None:
        return None
    age = (datetime.now(UTC) - last).total_seconds()
    if age < float(spec.interval_seconds):
        return None
    spread = min(stagger, OVERDUE_SPREAD_SECONDS)
    delay = float(zlib.crc32(spec.name.encode("utf-8")) % max(1, int(spread)))
    log.info(
        "%s: last pass %.1f h ago exceeds its %.1f h interval — "
        "first pass in %.0fs instead of the %.0fs boot stagger",
        spec.name,
        age / 3600,
        spec.interval_seconds / 3600,
        delay,
        stagger,
    )
    return delay


def first_pass_delay(spec: WorkerSpec) -> float:
    """Seconds to hold the first pass: `crc32(name) % interval` — stable
    across processes and restarts (Python's `hash()` is salted per process),
    0 for sub-2-second intervals so tests and hot loops start at once. The
    stagger keeps every worker's first read from hitting ClickHouse in one
    boot tick."""
    interval = int(spec.interval_seconds)
    if interval < 2:
        return 0.0
    return float(zlib.crc32(spec.name.encode("utf-8")) % interval)


def _begin_pass_accounting() -> Any:
    """Bind a fresh cost accumulator to this worker task (fail-quiet), and
    the ClickHouse work bag (statements, read bytes, elapsed)
    the pass's store row reads."""
    try:
        from server.core import request_work

        request_work.begin()
    except Exception:  # noqa: BLE001 — accounting never breaks the loop
        pass
    try:
        from server.observability import cost_accumulator

        return cost_accumulator.begin_op()
    except Exception:  # noqa: BLE001 — accounting never breaks the loop
        return None


def _after_pass(
    spec: WorkerSpec,
    acc: Any,
    ctx: pass_context.PassContext,
    state: LoopState,
    *,
    started: float,
    streak: int,
) -> int:
    """Record a pass that returned and give the new failure streak. A pass
    with a failed leg finished, but counts toward the streak (DEGRADED after
    three such passes) while the loop keeps its normal interval: the work
    the pass did complete is still wanted on time. A clean pass resets it."""
    partial = ctx.count
    _publish_partial(spec.name, partial)
    new_streak = streak + 1 if partial else 0
    _finish_pass(
        spec.name,
        acc,
        started=started,
        success=True,
        partial=ctx,
        spec=spec,
        state=state,
        streak=new_streak,
    )
    _publish_streak(spec.name, new_streak)
    if partial:
        first = ctx.first()
        log.warning(
            "%s: pass completed with %d failed leg(s) (consecutive_failures=%d): %s",
            spec.name,
            partial,
            new_streak,
            (f"{first[0]} {first[1]}: {first[2]}" if first else ""),
        )
        if new_streak >= _ALERT_THRESHOLD:
            log.error(
                "%s: DEGRADED — %d consecutive passes with a failed leg. "
                "Check the leg named above.",
                spec.name,
                new_streak,
            )
    elif streak > 0:
        log.info("%s: recovered after %d consecutive failures", spec.name, streak)
    return new_streak


def _finish_pass(
    name: str,
    acc: Any,
    *,
    started: float,
    success: bool,
    error_code: str = "",
    error: BaseException | None = None,
    partial: pass_context.PassContext | None = None,
    spec: WorkerSpec | None = None,
    state: LoopState | None = None,
    streak: int = 0,
    deadline: float = 0.0,
) -> None:
    """The two rows a finished pass leaves: `loom.worker_status` (health,
    where the web tier and the dashboard read it) and the
    `agent_events` ledger row (spend). Status first: the
    ledger row drains the cost accumulator the status row peeks at."""
    _record_status_row(
        name,
        started=started,
        success=success,
        error=error,
        partial=partial,
        spec=spec,
        state=state,
        streak=streak,
        deadline=deadline,
    )
    _emit_pass_row(
        name,
        acc,
        started=started,
        success=success,
        error_code=error_code,
        error=error,
        partial=partial,
        outcome=_pass_outcome(partial),
    )


def _pass_outcome(partial: pass_context.PassContext | None) -> dict[str, Any]:
    """The outcome this pass recorded (`record_pass` on a clean exit), or
    empty: a pass that raised or was cancelled carries no counts."""
    ctx = partial if partial is not None else pass_context.current()
    return dict(ctx.outcome or {}) if ctx is not None else {}


def _record_status_row(
    name: str,
    *,
    started: float,
    success: bool,
    error: BaseException | None = None,
    partial: pass_context.PassContext | None = None,
    spec: WorkerSpec | None = None,
    state: LoopState | None = None,
    streak: int = 0,
    deadline: float = 0.0,
    cancelled: bool = False,
) -> None:
    """Queue this pass's `loom.worker_status` row: status,
    error, the worker's counters (from the registry outcome the pass wrote),
    what it read and spent (the work bag and the cost accumulator, peeked
    before the ledger drains it), the trigger, the cursor and the last
    namespace the pass reached (pass_context.set_namespace). Idle passes
    write a row too. Never raises."""
    try:
        from server.core import request_work
        from server.observability import cost_accumulator

        outcome = _pass_outcome(partial)
        noted = partial.count if partial is not None else 0
        status = status_store.classify(
            success=success,
            partial_failures=noted,
            deadline_seconds=deadline,
            cancelled=cancelled,
            outcome=outcome,
        )
        reason = (
            f"pass exceeded its deadline of {deadline:.0f}s and was cancelled"
            if status == "timeout"
            else ""
        )
        degraded = streak if streak >= _ALERT_THRESHOLD else 0
        error_class, error_text = status_store.error_fields(
            error,
            reason=reason,
            partial=partial,
            degraded_streak=degraded,
        )
        planned, done, written = status_store.counts(outcome)
        bag = request_work.current()
        acc = cost_accumulator.current()
        pass_started, pass_finished = status_store.wall_from_monotonic(started)
        # The cursor this pass left (pass_context.set_cursor), whether or not
        # the caller handed the context over — it is still bound here.
        ctx = partial if partial is not None else pass_context.current()
        if state is not None and ctx is not None and ctx.cursor:
            state.cursor = ctx.cursor
        status_store.record_pass(
            name,
            status=status,
            started=pass_started,
            finished=pass_finished,
            error_class=error_class,
            error=error_text,
            deadline_s=spec.deadline_seconds() if spec is not None else 0.0,
            interval_s=spec.interval_seconds if spec is not None else 0.0,
            trigger=state.trigger if state is not None else "",
            consecutive_failures=streak,
            items_planned=planned,
            items_done=done,
            rows_written=written,
            statements=getattr(bag, "ch_statements", 0),
            read_bytes=getattr(bag, "ch_read_bytes", 0),
            ch_elapsed_ms=int(getattr(bag, "ch_elapsed_ms", 0) or 0),
            llm_calls=getattr(acc, "llm_calls", 0),
            spent_usd=getattr(acc, "cost_usd", 0.0),
            resume_cursor=state.cursor if state is not None else "",
            namespace=ctx.namespace if ctx is not None else "",
            partial_failures=noted,
            detail=status_store.detail_text(outcome, {"degraded": True} if degraded else None),
        )
        if state is not None:
            state.trigger = "interval" if success else "retry_backoff"
    except Exception:
        log.debug("%s: worker_status row failed", name, exc_info=True)


def _emit_pass_row(
    name: str,
    acc: Any,
    *,
    started: float,
    success: bool,
    error_code: str = "",
    error: BaseException | None = None,
    partial: pass_context.PassContext | None = None,
    outcome: dict[str, Any] | None = None,
) -> None:
    """One `agent_events` row per worker pass: operation
    `worker.<name>.pass`, kind='worker', with the model, tokens and dollars
    the pass spent, also ticked on the cost counters. A pass that spent nothing
    and did not fail writes no row, so idle intervals do not fill the
    table; loom.worker_status records every pass. Never
    raises."""
    try:
        from server.observability import cost_accumulator
        from server.settings import settings
        from server.workers import cost_event

        if not bool(settings.worker_ledger_rows):
            return
        # Drain whatever accumulator is bound NOW: a worker that begins its
        # own op inside the pass (entity_extractor, code_summary) has
        # replaced ours and drained it into its own row already, so this
        # reads zero for it and writes nothing — the spend appears once.
        bound = cost_accumulator.current()
        snap = bound.drain() if bound is not None else None
        did_spend = cost_event.spent(snap)
        noted = partial.count if partial is not None else 0
        if success and not did_spend and not noted:
            return
        # `.pass` keeps the supervisor's one-row-per-pass apart from the
        # per-item `worker.<name>` rows workers emit themselves; `LIKE
        # 'worker.%'` still sums both for spend.
        cost_event.emit_worker_event(
            snap,
            org="-",
            namespace="-",
            operation=f"worker.{name}.pass",
            started=started,
            success=success,
            error_code=error_code,
            args=_pass_args(name, spent=did_spend, error=error, partial=partial, outcome=outcome),
        )
    except Exception:
        log.debug("%s: pass ledger row failed", name, exc_info=True)


# At most this many outcome scalars ride the pass row.
_OUTCOME_ARGS_MAX = 16


def _pass_args(
    name: str,
    *,
    spent: bool,
    error: BaseException | None,
    partial: pass_context.PassContext | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, str]:
    """The pass row's args. A failed pass carries the same bounded error
    the request ops carry: class name plus the first line
    with quoted spans elided, cut at 200 characters, so a failure's cause is
    on its own row rather than only its class name."""
    args = {"worker": name, "spent": "true" if spent else "false"}
    if error is not None:
        try:
            from server.observability.event_args import bounded_error

            cls, msg = bounded_error(error)
            args["error_class"] = cls
            if msg:
                args["error"] = msg
        except Exception:  # noqa: BLE001 — the row goes out without the text
            args.setdefault("error_class", type(error).__name__)
    # Legs that failed inside a pass that otherwise finished.
    # The count, and the first failure's leg / class / bounded message under
    # `partial_*` so it never masks a pass-level `error`.
    if partial is not None and partial.count:
        args["partial_failures"] = str(partial.count)
        first = partial.first()
        if first is not None:
            what, cls, msg = first
            if what:
                args["partial_leg"] = what
            args["partial_error_class"] = cls
            if msg:
                args["partial_error"] = msg
    # The counts the pass recorded on its outcome (rows written, items, a
    # partial flag …) ride the row as `outcome_<key>`. Scalars only, bounded.
    for key, value in list((outcome or {}).items())[:_OUTCOME_ARGS_MAX]:
        if isinstance(value, bool | int | float | str):
            args[f"outcome_{key}"] = str(value)[:200]
    return args


def _publish_partial(name: str, count: int) -> None:
    """`loom.worker.partial_failures{worker}` — fail-quiet."""
    try:
        from server.observability import metrics_gauges as _gauges

        _gauges.set_worker_partial_failures(name, count)
    except Exception:  # noqa: BLE001 — telemetry never breaks the loop
        pass


def _publish_streak(name: str, count: int) -> None:
    """`loom.worker.consecutive_failures{worker}` — fail-quiet."""
    try:
        from server.observability import metrics_gauges as _gauges

        _gauges.set_worker_consecutive_failures(name, count)
    except Exception:  # noqa: BLE001 — telemetry never breaks the loop
        pass


# Module-level singleton — lifespan in server.main wires the start/stop.
_supervisor = WorkerSupervisor()


def get_supervisor() -> WorkerSupervisor:
    return _supervisor
