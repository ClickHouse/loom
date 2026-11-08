"""Worker health written to the store.

`server.workers.status` is the in-process registry: it answers "is the loop
healthy right now?" only for the process that runs the loop, and under
blue/green the web tier runs none.

This module is the store-side twin: one `loom.worker_status` row per finished
pass (idle passes included, so "ran and found nothing" is
observable), a reader that folds the table to the latest row per (worker,
instance_label) with `argMax`, the cursor a restarted pass reads, and the
merge that lets the admin handler show the freshest report per worker whether
it came from this process or from the colour that holds the lease.

Write path: rows queue in-process and one committed insert flushes them every
FLUSH_INTERVAL_S (or at FLUSH_ROWS), so the 2 s and 5 s workers do not each
write a one-row part per pass; the supervisor
drains the queue at shutdown. Every call here is fail-quiet: health reporting
never breaks the pass it reports on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from server.core.datetimes import UNIX_EPOCH

log = logging.getLogger(__name__)

TABLE = "loom.worker_status"
STATUSES = ("ok", "failed", "timeout", "partial", "cancelled", "idle")
# `manual`: an operator ran the pass outside the supervisor (`loom code
# backfill`, the scip-delta subcommand). `overdue`: the last pass is older than
# the interval, so the first pass starts within the overdue spread of boot.
TRIGGERS = ("interval", "first_pass", "overdue", "retry_backoff", "boot", "manual")

FLUSH_INTERVAL_S = 15.0
FLUSH_ROWS = 200
_DETAIL_LIMIT = 2000
_ERROR_LIMIT = 1000
_CURSOR_LIMIT = 512
_NAMESPACE_LIMIT = 256

# Registry outcome keys, by what they count. Workers report their own
# vocabulary in `rec.outcome`; these are the names in use today. A key that
# ends in one of these (`memories_embedded`) counts too.
_PLANNED_KEYS = (
    "planned",
    "total",
    "candidates",
    "queued",
    "pending",
    "due",
    "scanned",
    "considered",
)
_DONE_KEYS = (
    "processed",
    "done",
    "items",
    "count",
    "handled",
    "checked",
    "folded",
    "extracted",
    "indexed",
    "reconciled",
    "verified",
    "promoted",
    "swept",
    "embedded",
)
_WRITTEN_KEYS = (
    "rows",
    "written",
    "inserted",
    "updated",
    "upserted",
    "deleted",
    "tombstoned",
    "scrubbed",
    "applied",
    "created",
    "emitted",
)

# Column order of the insert; mirrors the table DDL.
COLUMNS = (
    "pass_id",
    "worker",
    "instance_label",
    "namespace",
    "process",
    "build_commit",
    "pass_started",
    "pass_finished",
    "status",
    "error_class",
    "error",
    "deadline_s",
    "interval_s",
    "trigger",
    "consecutive_failures",
    "items_planned",
    "items_done",
    "rows_written",
    "statements",
    "read_bytes",
    "ch_elapsed_ms",
    "llm_calls",
    "spent_usd",
    "resume_cursor",
    "partial_failures",
    "detail",
    "version",
)

_queue: list[dict[str, Any]] = []
_flusher: asyncio.Task[None] | None = None
# The early flush a full queue starts. Held here: the loop keeps only a weak
# reference to a task, so an unreferenced one can be collected mid-insert.
_flush_now_task: asyncio.Task[None] | None = None
_flushing: asyncio.Lock | None = None
_client_override: Any = None


# --------------------------------------------------------------------------- classify


def classify(
    *,
    success: bool,
    partial_failures: int = 0,
    deadline_seconds: float = 0.0,
    cancelled: bool = False,
    outcome: dict[str, Any] | None = None,
) -> str:
    """The pass's status: timeout (cancelled at its deadline),
    cancelled (the process stopped mid-pass), failed (the pass raised),
    partial (finished with a failed leg), idle (clean, counted nothing), ok."""
    if cancelled:
        return "cancelled"
    if deadline_seconds > 0 and not success:
        return "timeout"
    if not success:
        return "failed"
    if partial_failures > 0:
        return "partial"
    nums = _numeric(outcome)
    explicit = [nums[k] for k in COUNT_KEYS if k in nums]
    if explicit:
        # A worker that states its counts is idle when it
        # planned, did and wrote nothing — its other scalars (a wall clock,
        # namespaces visited) do not make a pass that did nothing "ok".
        return "idle" if all(v == 0 for v in explicit) else "ok"
    if nums and all(v == 0 for v in nums.values()):
        return "idle"
    return "ok"


# The row's three counters, stated on the outcome under their
# own names. A worker that writes them is read exactly; the name heuristic
# below (`_PLANNED_KEYS` …) is the fallback for the vocabulary in use today.
COUNT_KEYS = ("items_planned", "items_done", "rows_written")


def counts(outcome: dict[str, Any] | None) -> tuple[int, int, int]:
    """`(items_planned, items_done, rows_written)`: each read off the outcome's own
    `COUNT_KEYS` entry when the worker states it, else summed from the outcome's counters by
    key name — best effort; a worker with its own vocabulary reads 0 and its outcome is
    still in `detail`."""
    nums = _numeric(outcome)
    heuristics = (_PLANNED_KEYS, _DONE_KEYS, _WRITTEN_KEYS)
    planned, done, written = (
        max(int(nums[key]), 0) if key in nums else _sum_keys(nums, keys)
        for key, keys in zip(COUNT_KEYS, heuristics, strict=True)
    )
    return planned, done, written


def error_fields(
    error: BaseException | None,
    *,
    reason: str = "",
    partial: Any = None,
    degraded_streak: int = 0,
) -> tuple[str, str]:
    """`(error_class, error)` for the row: the bounded error (`event_args.bounded_error`)
    for a raised pass, the first failed leg for a partial one, the deadline reason for a
    timeout; a DEGRADED verdict (the supervisor's streak) is written in front so the
    operator reads it where the row is."""
    cls, msg = "", ""
    noted = int(getattr(partial, "count", 0) or 0) if partial is not None else 0
    if error is not None:
        try:
            from server.observability.event_args import bounded_error

            cls, msg = bounded_error(error)
        except Exception:  # noqa: BLE001 — the row goes out without the text
            cls, msg = type(error).__name__, ""
    elif noted > 0:
        first = partial.first()
        if first is not None:
            what, cls, msg = first
            msg = f"{what}: {msg}" if what else msg
    if reason:
        msg = f"{reason}; {msg}" if msg else reason
    if degraded_streak > 0:
        msg = (
            f"DEGRADED — {degraded_streak} consecutive failures; {msg}"
            if msg
            else (f"DEGRADED — {degraded_streak} consecutive failures")
        )
    return cls[:128], msg[:_ERROR_LIMIT]


def detail_text(outcome: dict[str, Any] | None, extra: dict[str, Any] | None = None) -> str:
    """The row's free text: the worker's outcome (+ extras) as JSON, bounded.
    A worker that stashes a list of reasons cannot blow up the row."""
    body: dict[str, Any] = {}
    for src in (outcome or {}, extra or {}):
        for k, v in src.items():
            if isinstance(v, bool | int | float | str) or v is None:
                body[str(k)] = v
            else:
                body[str(k)] = str(v)[:200]
    if not body:
        return ""
    try:
        return json.dumps(body, sort_keys=True, default=str)[:_DETAIL_LIMIT]
    except (TypeError, ValueError):
        return ""


# --------------------------------------------------------------------------- write


def build_row(
    worker: str,
    *,
    status: str,
    started: datetime,
    finished: datetime,
    **fields: Any,
) -> dict[str, Any]:
    """One `loom.worker_status` row in COLUMNS order. Unknown fields are
    ignored; missing ones take the migration's defaults. `version` is the
    finish time in nanoseconds so a re-sent row supersedes cleanly."""
    stamps = fields.pop("stamps", None) or _stamps()
    started, finished = _utc(started), _utc(finished)
    row: dict[str, Any] = {
        "pass_id": fields.get("pass_id") or uuid.uuid4(),
        "worker": worker,
        "instance_label": stamps.get("instance_label", ""),
        # The last namespace the pass reached ('' when
        # the worker has no notion of one) — where a timed-out pass stopped.
        "namespace": str(fields.get("namespace") or "")[:_NAMESPACE_LIMIT],
        "process": stamps.get("instance", ""),
        "build_commit": stamps.get("build_commit", ""),
        "pass_started": started.replace(tzinfo=None),
        "pass_finished": finished.replace(tzinfo=None),
        "status": status if status in STATUSES else "failed",
        "error_class": str(fields.get("error_class") or "")[:128],
        "error": str(fields.get("error") or "")[:_ERROR_LIMIT],
        "deadline_s": float(fields.get("deadline_s") or 0.0),
        "interval_s": float(fields.get("interval_s") or 0.0),
        "trigger": fields.get("trigger") if fields.get("trigger") in TRIGGERS else "",
        "consecutive_failures": _clamp(fields.get("consecutive_failures"), 65535),
        "items_planned": _clamp(fields.get("items_planned")),
        "items_done": _clamp(fields.get("items_done")),
        "rows_written": _clamp(fields.get("rows_written")),
        "statements": _clamp(fields.get("statements"), 2**32 - 1),
        "read_bytes": _clamp(fields.get("read_bytes")),
        "ch_elapsed_ms": _clamp(fields.get("ch_elapsed_ms"), 2**32 - 1),
        "llm_calls": _clamp(fields.get("llm_calls"), 2**32 - 1),
        "spent_usd": float(fields.get("spent_usd") or 0.0),
        "resume_cursor": str(fields.get("resume_cursor") or "")[:_CURSOR_LIMIT],
        "partial_failures": _clamp(fields.get("partial_failures"), 2**32 - 1),
        "detail": str(fields.get("detail") or "")[:_DETAIL_LIMIT],
        "version": int(finished.timestamp() * 1_000_000_000),
    }
    return row


def record_pass(
    worker: str,
    *,
    status: str,
    started: datetime,
    finished: datetime,
    **fields: Any,
) -> dict[str, Any] | None:
    """Queue one row for a finished pass (see `build_row` for fields) and make
    sure a flusher is running; the supervisor loop never waits on ClickHouse.
    Returns the queued row, or None when the row could not be built or no
    loop is running — never raises."""
    try:
        row = build_row(worker, status=status, started=started, finished=finished, **fields)
    except Exception:
        log.debug("%s: worker_status row not built", worker, exc_info=True)
        return None
    _queue.append(row)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("%s: no running loop; worker_status row queued unflushed", worker)
        return row
    _ensure_flusher(loop)
    if len(_queue) >= FLUSH_ROWS:
        _start_flush_now(loop)
    return row


def _start_flush_now(loop: asyncio.AbstractEventLoop) -> None:
    """One early flush at a time: the one in flight takes every queued row."""
    global _flush_now_task
    if _flush_now_task is not None and not _flush_now_task.done():
        return
    _flush_now_task = loop.create_task(_flush_now(), name="loom-worker-status:flush-now")


async def record_manual_pass(
    worker: str,
    *,
    namespace: str,
    status: str,
    started: datetime,
    finished: datetime,
    timeout_s: float = 10.0,
    **fields: Any,
) -> dict[str, Any] | None:
    """An operator-run pass (`loom code backfill`, `loom code scip-delta`)
    leaves its row with `trigger='manual'` and writes it NOW — the CLI
    process exits before a 15 s flusher would fire, so the queue is drained
    here. Fail-quiet like `record_pass`; returns the row (None if not built)."""
    fields.pop("trigger", None)
    row = record_pass(
        worker,
        status=status,
        started=started,
        finished=finished,
        namespace=namespace,
        trigger="manual",
        **fields,
    )
    if row is not None:
        await drain(timeout_s=timeout_s)
    return row


def _ensure_flusher(loop: asyncio.AbstractEventLoop) -> None:
    global _flusher
    if _flusher is not None and not _flusher.done():
        return
    _flusher = loop.create_task(_flush_forever(), name="loom-worker-status:flusher")


# The flush tasks are created inside whichever pass queued a row, and a task
# copies its creator's context: untagged, every worker's status insert would be
# logged under the query tag of the worker that happened to start the flusher.
_FLUSH_QUERY_TAG = "worker_status.flush"


def _tag_flush_task() -> None:
    from server.core.ch import set_query_tag

    set_query_tag(_FLUSH_QUERY_TAG)


async def _flush_now() -> None:
    _tag_flush_task()
    await flush()


async def _flush_forever() -> None:
    _tag_flush_task()
    try:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            await flush()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("worker_status: flusher stopped", exc_info=True)


async def flush(*, ch: Any = None, timeout_s: float = 10.0) -> int:
    """Write every queued row in one committed insert; returns rows written.
    A failure is one WARNING and the rows are dropped (the registry and the
    ledger row still carry the pass; a queue that grows on an outage is not
    a memory leak the pass loop should carry)."""
    global _flushing
    if _flushing is None:
        _flushing = asyncio.Lock()
    async with _flushing:
        rows, _queue[:] = list(_queue), []
        if not rows:
            return 0
        try:
            client = ch if ch is not None else _client()
            async with asyncio.timeout(timeout_s):
                await client.insert(TABLE, [{k: r.get(k) for k in COLUMNS} for r in rows])
            return len(rows)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the loop must not learn of this
            log.warning(
                "worker_status: %d row(s) not written (%s: %s)",
                len(rows),
                type(e).__name__,
                str(e)[:200],
            )
            return 0


async def drain(timeout_s: float = 5.0) -> None:
    """Flush now and stop the flusher; the supervisor calls this on shutdown
    so the last pass of a stopping colour reaches the store."""
    global _flusher
    if _flusher is not None:
        _flusher.cancel()
        _flusher = None
    try:
        await flush(timeout_s=timeout_s)
    except Exception:
        log.debug("worker_status: drain failed", exc_info=True)


def queued() -> int:
    return len(_queue)


def reset_for_tests(*, client: Any = None) -> None:
    """Empty the queue, forget the flushers and (optionally) bind a fake client."""
    global _flusher, _flush_now_task, _client_override
    _queue.clear()
    for task in (_flusher, _flush_now_task):
        if task is not None:
            task.cancel()
    _flusher = _flush_now_task = None
    _client_override = client


# --------------------------------------------------------------------------- read


# The freshest row per (worker, instance_label). Every column rides an
# argMax on `version`; the aggregate over version itself is NOT aliased to
# `version`, and the two timestamps are NOT aliased to their source columns
# either — `pass_started` is in the WHERE, and ClickHouse resolves the alias
# there (code 184 ILLEGAL_AGGREGATION; code_repo has the same note). No FINAL: argMax reads the same
# on every engine (Cloud refuses FINAL on some SharedMergeTree tables).
LATEST_SQL = f"""
SELECT
    worker,
    instance_label,
    argMax(namespace, version) AS namespace,
    argMax(process, version) AS process,
    argMax(build_commit, version) AS build_commit,
    argMax(pass_started, version) AS last_started,
    argMax(pass_finished, version) AS last_finished,
    argMax(status, version) AS status,
    argMax(error_class, version) AS error_class,
    argMax(error, version) AS error,
    argMax(deadline_s, version) AS deadline_s,
    argMax(interval_s, version) AS interval_s,
    argMax(trigger, version) AS trigger,
    argMax(consecutive_failures, version) AS consecutive_failures,
    argMax(items_planned, version) AS items_planned,
    argMax(items_done, version) AS items_done,
    argMax(rows_written, version) AS rows_written,
    argMax(statements, version) AS statements,
    argMax(llm_calls, version) AS llm_calls,
    argMax(spent_usd, version) AS spent_usd,
    argMax(partial_failures, version) AS partial_failures,
    argMax(detail, version) AS detail,
    count() AS passes_kept,
    max(version) AS latest_version
FROM {TABLE}
WHERE pass_started > now() - toIntervalDay({{days:UInt32}})
GROUP BY worker, instance_label
ORDER BY worker, instance_label
"""

# The cursor a restarted pass reads: the latest non-empty one this worker
# left, from any process (a new colour continues where the old one stopped).
RESUME_CURSOR_SQL = f"""
SELECT argMax(resume_cursor, version) AS cursor
FROM {TABLE}
WHERE worker = {{worker:String}} AND resume_cursor != ''
"""


async def latest_rows(*, ch: Any = None, days: int = 7) -> list[dict[str, Any]]:
    """Latest pass per (worker, instance_label) within `days`, as plain dicts
    with ISO timestamps. Empty on any error (one WARNING) — the handler still
    answers from the registry."""
    try:
        client = ch if ch is not None else _client()
        rows = await client.query_all(LATEST_SQL, {"days": max(int(days), 1)})
    except Exception as e:  # noqa: BLE001 — the admin read must not 500 on the store
        log.warning("worker_status: latest rows not read (%s: %s)", type(e).__name__, str(e)[:200])
        return []
    out: list[dict[str, Any]] = []
    for r in rows or []:
        d = dict(r)
        d["pass_started"] = _iso(d.pop("last_started", None) or d.get("pass_started"))
        d["pass_finished"] = _iso(d.pop("last_finished", None) or d.get("pass_finished"))
        out.append(d)
    return out


# When this worker last finished a pass in this process's
# deployment family (every colour of `deploy:workers:*`, never an iso or a
# bench server's rows), so a restart does not restart a long interval.
LAST_PASS_SQL = f"""
SELECT max(pass_finished) AS finished
FROM {TABLE}
WHERE worker = {{worker:String}} AND startsWith(instance_label, {{family:String}})
"""


def label_family(label: str) -> str:
    """`deploy:workers:green` → `deploy:workers:` (every colour of the
    deployment); any other label is its own family, exactly."""
    parts = str(label or "").split(":")
    if len(parts) >= 3 and parts[0] == "deploy":
        return ":".join(parts[:-1]) + ":"
    return str(label or "")


async def last_pass_finished(
    worker: str, family: str, *, ch: Any = None, timeout_s: float = 5.0
) -> datetime | None:
    """The newest `pass_finished` of `worker` in `family`; None when there is
    none or the store cannot answer (the caller then keeps the stagger)."""
    try:
        client = ch if ch is not None else _client()
        async with asyncio.timeout(timeout_s):
            row = await client.query_one(LAST_PASS_SQL, {"worker": worker, "family": family})
        value = (row or {}).get("finished")
        if not isinstance(value, datetime) or value.year < 2000:
            return None
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    except Exception as e:  # noqa: BLE001 — unknown reads as "not overdue"
        log.debug("%s: last pass not read (%s: %s)", worker, type(e).__name__, e)
        return None


async def resume_cursor(worker: str, *, ch: Any = None, timeout_s: float = 5.0) -> str:
    """The cursor the last pass of `worker` left ('' when none, or when the
    store cannot answer — a restarted pass then starts from the beginning)."""
    try:
        client = ch if ch is not None else _client()
        async with asyncio.timeout(timeout_s):
            row = await client.query_one(RESUME_CURSOR_SQL, {"worker": worker})
        return str((row or {}).get("cursor") or "")[:_CURSOR_LIMIT]
    except Exception as e:  # noqa: BLE001 — a missing cursor is a fresh start
        log.debug("%s: resume cursor not read (%s: %s)", worker, type(e).__name__, e)
        return ""


# One row per worker whose name starts with {prefix}: its newest pass and
# how many passes the window holds (a plane's status panel, which a viewer reads:
# the error's class, never its text). `pass_started` is not aliased: it is in the WHERE.
PASS_SUMMARY_SQL = f"""
SELECT worker,
       max(pass_started) AS last_started,
       argMax(pass_finished, pass_started) AS last_finished,
       argMax(error_class, pass_started) AS last_error_class,
       argMax(status, pass_started) AS last_status,
       argMax(consecutive_failures, pass_started) AS consecutive_failures,
       count() AS total_runs
FROM {TABLE}
WHERE startsWith(worker, {{prefix:String}})
  AND pass_started >= now() - INTERVAL {{days:UInt16}} DAY
GROUP BY worker
"""


async def pass_summaries(prefix: str, days: int, *, ch: Any = None) -> list[dict[str, Any]]:
    """`PASS_SUMMARY_SQL` rows for the workers named `prefix*` over `days`.
    Raises what the read raises; the caller decides what a missing ledger means."""
    client = ch if ch is not None else _client()
    return list(await client.query_all(PASS_SUMMARY_SQL, {"prefix": prefix, "days": int(days)}))


# --------------------------------------------------------------------------- merge


def merge_status(
    base: dict[str, dict[str, Any]],
    store_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Fold the store's latest rows into the handler's per-worker dicts (the
    registry snapshot over `worker_meta()`). The freshest report per worker
    wins — this process's registry when it ran the pass more recently, the
    store row otherwise — and every row says where it came from
    (`reporting_from`, `source`). A worker the store knows and this build's
    meta does not (a newer colour) is added, so the list is the union."""
    best: dict[str, dict[str, Any]] = {}
    reporters: dict[str, set[str]] = {}
    for r in store_rows:
        name = str(r.get("worker") or "")
        if not name:
            continue
        reporters.setdefault(name, set()).add(str(r.get("instance_label") or ""))
        cur = best.get(name)
        if cur is None or (_parse(r.get("pass_finished")) or UNIX_EPOCH) > (
            _parse(cur.get("pass_finished")) or UNIX_EPOCH
        ):
            best[name] = r
    out: dict[str, dict[str, Any]] = {}
    for name, row in base.items():
        out[name] = _merge_one(dict(row), best.get(name), reporters.get(name, set()))
    for name, r in best.items():
        if name not in out:
            out[name] = _merge_one(
                {
                    "enabled": True,
                    "effective": True,
                    "interval_seconds": r.get("interval_s") or None,
                    "disabled_reason": None,
                    "disabled_detail": None,
                },
                r,
                reporters.get(name, set()),
            )
    return out


def _merge_one(
    row: dict[str, Any],
    store: dict[str, Any] | None,
    labels: set[str],
) -> dict[str, Any]:
    reg_at = _parse(row.get("last_run_at"))
    store_at = _parse(store.get("pass_finished")) if store else None
    row["reporters"] = sorted(labels)
    if store is not None and store_at is not None and (reg_at is None or store_at > reg_at):
        started = _parse(store.get("pass_started"))
        row["last_run_at"] = _iso(store_at)
        row["last_duration_ms"] = (
            int((store_at - started).total_seconds() * 1000) if started is not None else None
        )
        row["last_error"] = str(store.get("error") or "") or None
        row["last_error_kind"] = str(store.get("error_class") or "") or None
        row["consecutive_failures"] = int(store.get("consecutive_failures") or 0)
        row["last_pass_status"] = str(store.get("status") or "ok")
        row["rows_written"] = int(store.get("rows_written") or 0)
        row["items_done"] = int(store.get("items_done") or 0)
        row["detail"] = str(store.get("detail") or "")
        row["reporting_from"] = str(store.get("instance_label") or "") or "unlabelled process"
        # Where the pass reached and why it ran.
        row["namespace"] = str(store.get("namespace") or "")
        row["trigger"] = str(store.get("trigger") or "")
        row["process"] = str(store.get("process") or "")
        row["build_commit"] = str(store.get("build_commit") or "")
        row["source"] = "store"
        return row
    if reg_at is not None:
        row["last_pass_status"] = "failed" if row.get("last_error") else "ok"
        row["reporting_from"] = _own_label()
        row["source"] = "registry"
        return row
    row["source"] = None
    return row


# --------------------------------------------------------------------------- helpers


def _client() -> Any:
    if _client_override is not None:
        return _client_override
    from server.core.ch import get_client

    return get_client()


def _stamps() -> dict[str, str]:
    try:
        from server.observability.instance import stamps

        return stamps()
    except Exception:  # noqa: BLE001 — a stamp never raises
        return {"instance_label": "", "instance": "", "build_commit": ""}


def _own_label() -> str:
    return _stamps().get("instance_label", "") or "this process"


def _numeric(outcome: dict[str, Any] | None) -> dict[str, int | float]:
    if not outcome:
        return {}
    return {
        str(k): v
        for k, v in outcome.items()
        if isinstance(v, int | float) and not isinstance(v, bool)
    }


def _sum_keys(nums: dict[str, int | float], keys: tuple[str, ...]) -> int:
    total = 0
    for k, v in nums.items():
        if k in keys or any(k.endswith(f"_{s}") for s in keys):
            total += int(v)
    return max(total, 0)


def _clamp(value: Any, cap: int = 2**64 - 1) -> int:
    try:
        return max(min(int(value or 0), cap), 0)
    except (TypeError, ValueError):
        return 0


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _iso(value: Any) -> str | None:
    dt = _parse(value)
    return dt.isoformat() if dt is not None else None


def _parse(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def wall_from_monotonic(started_monotonic: float) -> tuple[datetime, datetime]:
    """`(started, finished)` wall-clock for a pass timed with time.monotonic()."""
    finished = datetime.now(UTC)
    elapsed = max(time.monotonic() - started_monotonic, 0.0)
    return datetime.fromtimestamp(finished.timestamp() - elapsed, tz=UTC), finished


__all__ = [
    "COLUMNS",
    "COUNT_KEYS",
    "LATEST_SQL",
    "RESUME_CURSOR_SQL",
    "STATUSES",
    "TABLE",
    "TRIGGERS",
    "build_row",
    "classify",
    "counts",
    "detail_text",
    "drain",
    "error_fields",
    "flush",
    "latest_rows",
    "merge_status",
    "queued",
    "record_manual_pass",
    "record_pass",
    "reset_for_tests",
    "resume_cursor",
    "wall_from_monotonic",
]
