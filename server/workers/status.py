"""In-process worker status registry.

Each periodic worker records the result of its most recent pass here so
the `/v1/admin/workers.status` HTTP endpoint can surface it to an ops
dashboard without scraping logs.

Snapshot-only — no history. For history, use loom.audit_log or
loom.agent_events; this registry exists to answer "is the loop healthy
right now and when did it last run?".
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from server.workers import pass_context

log = logging.getLogger(__name__)


@dataclass
class WorkerStatus:
    name: str
    last_run_at: datetime | None = None
    last_duration_ms: int | None = None
    last_outcome: dict[str, Any] = field(default_factory=dict)  # worker-specific summary
    last_error: str | None = None  # truncated message; None when last run succeeded
    # The last failure's class, never its message, which can name an org; None on success.
    last_error_kind: str | None = None
    consecutive_failures: int = 0
    total_runs: int = 0


class WorkerStatusRegistry:
    """Thread-safe single-process registry. Workers are async-only but the
    registry uses a plain lock so non-async callers (the HTTP handler) can
    read safely without async machinery."""

    def __init__(self) -> None:
        self._statuses: dict[str, WorkerStatus] = {}
        self._lock = threading.Lock()

    def record_success(
        self,
        name: str,
        *,
        duration_ms: int,
        outcome: dict[str, Any],
    ) -> None:
        with self._lock:
            s = self._statuses.setdefault(name, WorkerStatus(name=name))
            s.last_run_at = datetime.now(UTC)
            s.last_duration_ms = duration_ms
            s.last_outcome = outcome
            s.last_error = None
            s.last_error_kind = None
            s.consecutive_failures = 0
            s.total_runs += 1

    def record_failure(self, name: str, *, duration_ms: int, exc: BaseException) -> None:
        """A failed pass: `last_error` keeps the text for `/v1/admin/workers.status`;
        `last_error_kind` is the class only, since the registry is process-wide and a
        Loom error's message can name another org."""
        with self._lock:
            s = self._statuses.setdefault(name, WorkerStatus(name=name))
            s.last_run_at = datetime.now(UTC)
            s.last_duration_ms = duration_ms
            s.last_error = f"{type(exc).__name__}: {exc}"[:512]
            s.last_error_kind = type(exc).__name__
            s.consecutive_failures += 1
            s.total_runs += 1

    def record_private(self, name: str, *, started: datetime, code: str) -> None:
        """Retain a private pass's code without duration, outcome or failure counters."""
        with self._lock:
            s = self._statuses.setdefault(name, WorkerStatus(name=name))
            s.last_run_at = started
            s.last_duration_ms = None
            s.last_outcome = {"code": code}
            s.last_error = None if code == "ok" else code
            s.last_error_kind = None if code == "ok" else code
            s.consecutive_failures = 0
            s.total_runs += 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a JSON-serializable view of all worker statuses."""
        with self._lock:
            out: dict[str, dict[str, Any]] = {}
            for name, s in self._statuses.items():
                d = asdict(s)
                if s.last_run_at is not None:
                    d["last_run_at"] = s.last_run_at.isoformat()
                out[name] = d
            return out

    def reset(self) -> None:
        """Test hook."""
        with self._lock:
            self._statuses.clear()


_registry = WorkerStatusRegistry()


def get_registry() -> WorkerStatusRegistry:
    return _registry


def shown_worker_row(row: dict[str, Any]) -> dict[str, Any]:
    """A registry row as a caller other than the platform admin reads it: without
    `last_error`, its outcome cut to numbers and flags. The registry is process-wide,
    so a pass's text and an outcome's strings can name another org."""
    out = {k: v for k, v in row.items() if k != "last_error"}
    if "last_outcome" in row:
        outcome = row["last_outcome"] or {}
        out["last_outcome"] = {
            k: v for k, v in outcome.items() if isinstance(v, bool | int | float)
        }
    return out


@dataclass
class _PassRecord:
    outcome: dict[str, Any] = field(default_factory=dict)


@contextmanager
def record_pass(name: str, *, trace_pass: bool = True) -> Iterator[_PassRecord]:
    """Record a worker pass, retaining codes only after it touched private work.

    A worker that can touch private sources omits its root span with
    ``trace_pass=False``: a span opened before that work cannot hide its duration.
    """
    rec = _PassRecord()
    started = time.monotonic()
    wall_started = datetime.now(UTC)
    set_worker_identity(name, fresh=True)
    with pass_context.ensure_pass(name):
        with _pass_span(name) if trace_pass else nullcontext() as span:
            try:
                yield rec
            except Exception as e:
                _record_result(name, started, wall_started, rec, span, error=e)
                raise
            _record_result(name, started, wall_started, rec, span)


def _record_result(
    name: str,
    started: float,
    wall_started: datetime,
    rec: _PassRecord,
    span: Any,
    *,
    error: BaseException | None = None,
) -> None:
    ctx = pass_context.current()
    if ctx is not None and ctx.private_work:
        code = "SYNC_FAILED" if error is not None or ctx.count else "ok"
        get_registry().record_private(name, started=wall_started, code=code)
        pass_context.record_outcome(name, {"code": code})
        return
    duration_ms = int((time.monotonic() - started) * 1000)
    if error is not None:
        get_registry().record_failure(name, duration_ms=duration_ms, exc=error)
    else:
        get_registry().record_success(name, duration_ms=duration_ms, outcome=rec.outcome)
        pass_context.record_outcome(name, rec.outcome)
    _finish_span(
        span, success=error is None, duration_ms=duration_ms, error=error, outcome=rec.outcome
    )
    _record_metrics(name, success=error is None, duration_ms=duration_ms)


def _pass_span(name: str) -> Any:
    try:
        from server.observability import otel as _otel

        return _otel.worker_pass_span(name)
    except Exception:  # noqa: BLE001 — telemetry must never fail a pass
        import contextlib

        return contextlib.nullcontext()


def _finish_span(
    span: Any,
    *,
    success: bool,
    duration_ms: int,
    outcome: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    if span is None:
        return
    try:
        span.set_attribute("loom.worker.success", success)
        span.set_attribute("loom.worker.duration_ms", duration_ms)
        # Outcome counters as span attributes, bounded to scalars so a
        # worker that stashes a list (skipped reasons) cannot blow up the row.
        for k, v in (outcome or {}).items():
            if isinstance(v, bool | int | float):
                span.set_attribute(f"loom.worker.outcome.{k}", v)
            elif isinstance(v, str) and v:
                span.set_attribute(f"loom.worker.outcome.{k}", v[:200])
        if error is not None:
            from opentelemetry.trace import Status, StatusCode

            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, f"{type(error).__name__}: {error}"[:200]))
    except Exception:  # noqa: BLE001 — span decoration never breaks the pass
        pass


def _record_metrics(name: str, *, success: bool, duration_ms: int) -> None:
    try:
        from server.observability import metrics as _metrics

        _metrics.record_worker_pass(name, success=success, duration_ms=duration_ms)
    except Exception:  # noqa: BLE001 — metrics never break the pass
        pass


def set_worker_identity(
    name: str, *, org: str = "", namespace: str = "", fresh: bool = False
) -> None:
    """Attribute this task's provider spans to ``worker.<name>`` (+ tenant).

    Thin wrapper so workers import one module for status + identity; the
    request-context guard lives in `genai.set_worker_identity`.
    """
    try:
        # A pass's ClickHouse queries are attributed to the worker.
        from server.core.ch import set_query_tag as _set_query_tag

        _set_query_tag(f"worker.{name}")
    except Exception:  # noqa: BLE001 — attribution never blocks a pass
        pass
    try:
        from server.observability import genai as _genai

        _genai.set_worker_identity(name, org=org, namespace=namespace, fresh=fresh)
    except Exception:  # noqa: BLE001 — span attribution never blocks a pass
        pass


def log_pass_errors(
    logger: logging.Logger, worker: str, errors: list[str], *, first: int = 3
) -> None:
    """One WARNING per pass summarising ``result.errors``.

    Workers collect per-repo/per-item errors in a list; a count in
    ``rec.outcome`` alone would discard WHICH repo 404'd and WHICH commit
    failed. Count plus the first few messages here; the full list stays on
    the result for callers that want it.
    """
    if not errors:
        return
    try:
        shown = "; ".join(str(e)[:300] for e in errors[:first])
        more = f" (+{len(errors) - first} more)" if len(errors) > first else ""
        logger.warning("%s: %d error(s) this pass: %s%s", worker, len(errors), shown, more)
    except Exception:  # noqa: BLE001 — error logging never breaks the pass
        pass


__all__ = [
    "WorkerStatus",
    "WorkerStatusRegistry",
    "get_registry",
    "log_pass_errors",
    "record_pass",
    "set_worker_identity",
]
