"""The Postgres authority's publication pipeline, as metrics (docs/operations/monitoring.md,
"Pipeline SLOs").

- `loom.pg.search_wait_ms`: what a read paid at the barrier when ClickHouse was behind
  (`pg_validation.admit`), by op, outcome and traffic class: the user-visible time
  from a write to its being searchable. It stops at LOOM_PG_PROJECTION_WAIT_MS
  (`outcome=deadline`): a search then answers 503, a report that may skip it reads on.
- `loom.pg.projection_lag_ms`: from a revision's commit to its last search target
  landing, measured on Postgres's clock as the delivery clears (`pg_memory._complete_in`),
  by path: the writer's own flush or a redelivery.
- `loom.pg.backlog` {source, state} and `loom.pg.oldest_pending_age_s` {source}: one
  reading per delivery pass, taken first in the pass by the worker that owns the source,
  exported only by the process holding the workers lease, dropped once older than two
  export periods (`loom.pg.backlog_reading_age_s` says how old it is).
- `loom.ch.inserts` and `loom.ch.insert_rows` {table}: every insert of memory rows, search
  documents and audit rows, under either authority, which drive ClickHouse's part creation.

Labels are closed enums; none names a namespace, a key or a user.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from server.settings import settings

_search_wait: Any = None
_projection_lag: Any = None
_inserts: Any = None
_insert_rows: Any = None

SOURCES = ("memory", "audit_outbox", "knowledge")
TABLES = frozenset({"memory", "memory_embeddings", "audit_log"})


@dataclass(frozen=True)
class Backlog:
    """One source's backlog: deliveries owed and not parked, parked ones, and the age of
    the oldest one owed (None for none). `capped`: the pending or the parked count
    reached the reading's cap."""

    pending: int
    parked: int
    oldest_age_s: float | None
    capped: bool = False


_readings: dict[str, tuple[Backlog, float]] = {}
_lock = threading.Lock()


def set_backlog(source: str, reading: Backlog) -> None:
    """Publish a source's reading for the gauges. Never raises."""
    try:
        with _lock:
            _readings[source] = (reading, time.monotonic())
    except Exception:  # noqa: BLE001 — telemetry must never fail the caller
        pass


def fresh_readings() -> dict[str, tuple[Backlog, float]]:
    """The readings younger than two export periods, each with its age in seconds."""
    limit = 2 * float(settings.otel_metrics_export_interval_s)
    now = time.monotonic()
    with _lock:
        return {s: (r, now - at) for s, (r, at) in _readings.items() if now - at <= limit}


def fresh_backlog() -> dict[str, Backlog]:
    """The readings younger than two export periods."""
    return {s: r for s, (r, _) in fresh_readings().items()}


def _reading_ages() -> dict[str, float]:
    now = time.monotonic()
    with _lock:
        return {s: now - at for s, (_, at) in _readings.items()}


def record_search_wait(outcome: str, wait_ms: float) -> None:
    """A read that waited at the barrier, under the op the request invoked (a closed set:
    the registry's). Never raises."""
    try:
        if _search_wait is None:
            return
        from server.observability.genai import current_operation
        from server.observability.traffic_class import current_traffic_class

        _search_wait.record(
            wait_ms,
            attributes={
                "loom.operation": current_operation() or "internal",
                "outcome": outcome,
                "loom.traffic_class": current_traffic_class() or "unclassified",
            },
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail a read
        pass


def record_projection_lag(lag_ms: float, *, redelivered: bool) -> None:
    """A revision whose last search target landed. Never raises."""
    try:
        if _projection_lag is not None:
            _projection_lag.record(
                max(0.0, lag_ms), attributes={"path": "redelivered" if redelivered else "flush"}
            )
    except Exception:  # noqa: BLE001 — telemetry must never fail a publish
        pass


def record_insert(table: str, rows: int) -> None:
    """One ClickHouse insert a publisher made. Never raises."""
    try:
        attrs = {"table": table if table in TABLES else "other"}
        if _inserts is not None:
            _inserts.add(1, attributes=attrs)
        if _insert_rows is not None:
            _insert_rows.add(max(0, int(rows)), attributes=attrs)
    except Exception:  # noqa: BLE001 — telemetry must never fail a publish
        pass


def register(meter: Any) -> None:
    """The instruments, on `meter`."""
    global _search_wait, _projection_lag, _inserts, _insert_rows
    _search_wait = meter.create_histogram(
        name="loom.pg.search_wait_ms",
        description="Time a read waited at the Postgres barrier for ClickHouse to hold "
        "the namespace's writes, by op (loom.operation), outcome "
        "(passed|deadline) and traffic class; only reads that found it behind",
        unit="ms",
    )
    _projection_lag = meter.create_histogram(
        name="loom.pg.projection_lag_ms",
        description="From a revision's commit to its last search target landing in "
        "ClickHouse, on Postgres's clock, by path (flush|redelivered)",
        unit="ms",
    )
    _inserts = meter.create_counter(
        name="loom.ch.inserts",
        description="ClickHouse inserts of memory rows, search documents and audit rows, "
        "by table (memory|memory_embeddings|audit_log): what drives part creation",
        unit="1",
    )
    _insert_rows = meter.create_counter(
        name="loom.ch.insert_rows",
        description="Rows in those inserts, by table",
        unit="1",
    )
    from server.observability import otel_probes

    def _backlog(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        if not otel_probes._census_reads_here():
            return []
        out = []
        for source, r in fresh_backlog().items():
            out.append(Observation(r.pending, {"source": source, "state": "pending"}))
            out.append(Observation(r.parked, {"source": source, "state": "parked"}))
        return out

    def _oldest(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        if not otel_probes._census_reads_here():
            return []
        return [
            Observation(r.oldest_age_s, {"source": source})
            for source, r in fresh_backlog().items()
            if r.oldest_age_s is not None
        ]

    def _ages(_options: Any) -> Any:
        from opentelemetry.metrics import Observation

        if not otel_probes._census_reads_here():
            return []
        return [Observation(age, {"source": s}) for s, age in _reading_ages().items()]

    meter.create_observable_gauge(
        name="loom.pg.backlog",
        callbacks=[_backlog],
        description="Deliveries owed to ClickHouse by source (memory|audit_outbox|knowledge) "
        "and state (pending|parked), from the last delivery pass",
        unit="1",
    )
    meter.create_observable_gauge(
        name="loom.pg.oldest_pending_age_s",
        callbacks=[_oldest],
        description="Age of the oldest delivery owed and not parked, by source (memory: "
        "those a search waits for)",
        unit="s",
    )
    meter.create_observable_gauge(
        name="loom.pg.backlog_reading_age_s",
        callbacks=[_ages],
        description="Seconds since the backlog reading of each source was taken",
        unit="s",
    )
