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
from contextlib import contextmanager
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
            s.consecutive_failures = 0
            s.total_runs += 1

    def record_failure(
        self,
        name: str,
        *,
        duration_ms: int,
        error: str,
    ) -> None:
        with self._lock:
            s = self._statuses.setdefault(name, WorkerStatus(name=name))
            s.last_run_at = datetime.now(UTC)
            s.last_duration_ms = duration_ms
            s.last_error = error[:512]
            s.consecutive_failures += 1
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


@dataclass
class _PassRecord:
    outcome: dict[str, Any] = field(default_factory=dict)


@contextmanager
def record_pass(name: str) -> Iterator[_PassRecord]:
    """Time one worker pass and record it to the registry: ``record_failure``
    + re-raise on exception, ``record_success(outcome=rec.outcome)`` on clean
    exit. Set ``rec.outcome`` inside the block before it ends.

    Also the worker plane's telemetry hook, since the registry is only a
    snapshot: each pass emits ``loom.worker.passes{worker, outcome}``
    and ``loom.worker.pass_duration_ms{worker}`` and runs inside a root span
    ``worker.<name>.pass`` (no request parent exists here) that the pass's
    CH/LLM/embedding spans nest under. Every telemetry call is fail-quiet;
    the registry update and the re-raise are what a worker relies on.
    """
    rec = _PassRecord()
    started = time.monotonic()
    set_worker_identity(name, fresh=True)
    with _pass_span(name) as span:
        try:
            yield rec
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            get_registry().record_failure(
                name, duration_ms=duration_ms, error=f"{type(e).__name__}: {e}"
            )
            _finish_span(span, success=False, duration_ms=duration_ms, error=e)
            _record_metrics(name, success=False, duration_ms=duration_ms)
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        get_registry().record_success(name, duration_ms=duration_ms, outcome=rec.outcome)
        pass_context.record_outcome(name, rec.outcome)
        _finish_span(span, success=True, duration_ms=duration_ms, outcome=rec.outcome)
        _record_metrics(name, success=True, duration_ms=duration_ms)


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
