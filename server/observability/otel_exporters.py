"""The ClickHouse OTel exporters: spans, logs and metrics INSERTed straight into
ClickStack's `default.otel_*` tables, plus the sync client they write through.

`otel.py` builds the providers around these; the row shapes match what
ClickStack's own collector writes, so the HyperDX UI renders them natively.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)

OTEL_EXPORTER_QUERY_TAG = "otel.exporter"


def _make_ch_client_for_otel() -> Any:
    """Build a sync clickhouse-connect client for OTel batch writes.

    Batch processors call exporters on a background thread, so the client
    must be sync — asyncio drivers don't survive being called outside
    their event loop.
    """
    import clickhouse_connect

    from server.core.ch_target import connection_kwargs

    return clickhouse_connect.get_client(
        **connection_kwargs(database="default"),  # ClickStack tables live in `default`
        # async_insert=0: rows visible immediately in HyperDX.
        # insert_deduplicate=0: identical events aren't dropped.
        settings={
            "async_insert": 0,
            "wait_for_async_insert": 1,
            "insert_deduplicate": 0,
            # This client bypasses server.core.ch, so it carries its own tag: every
            # exporter insert is attributed in system.query_log. Gauge probes
            # override it per query (`otel_probe_client._ProbeClient._call`).
            "log_comment": OTEL_EXPORTER_QUERY_TAG,
        },
    )


def _disabled_level() -> int:
    """How loudly an exporter says it disabled itself: a warning when the
    ClickHouse exporter was chosen, otherwise (the default, with no ClickStack)
    an expected note."""
    from server.settings import settings

    return logging.WARNING if settings.otel_exporter == "ch" else logging.INFO


def _unwritable(exc: BaseException) -> str:
    """Why a `default.otel_*` target can never take a write, or '' for an error
    worth retrying. The tables are created by ClickStack/HyperDX, not by Loom's
    migrations: on a ClickHouse-only deployment they are absent (code 60), and
    a Loom user scoped to `loom.*` may not write them (code 497). Either way the
    exporter disables itself after one line instead of failing every flush."""
    from server.core.ch_errors import error_code

    code = error_code(exc)
    if code == 60 or "UNKNOWN_TABLE" in str(exc):
        return "it does not exist (ClickStack/HyperDX is not deployed against this cluster)"
    if code == 497:
        return "this ClickHouse user may not access it"
    return ""


def _traces_backend_available(exporter_mode: str, endpoint: str | None) -> bool:
    """True only when spans have somewhere to go: an OTLP endpoint, or (ch mode)
    the ClickStack `default.otel_traces` table actually exists.

    Gates FastAPI auto-instrumentation. Without a backend, instrumenting is pure
    overhead AND — on the opentelemetry-instrumentation-asgi version CI resolves
    — crashes 500 while deriving a span name for the CORS OPTIONS preflight
    ('_IncludedRouter' object has no attribute 'path'). No backend ⇒ don't
    instrument. Any probe error (absent table, CH unreachable) ⇒ off, fail-safe.
    """
    if exporter_mode == "otlp":
        return bool(endpoint)
    try:
        client = _make_ch_client_for_otel()
        try:
            return str(client.command("EXISTS TABLE default.otel_traces")).strip() == "1"
        finally:
            client.close()
    except Exception:  # noqa: BLE001 — probe: any failure means no table
        return False


def _make_clickhouse_span_exporter(service_name: str) -> Any:
    """Build a SpanExporter that INSERTs into `default.otel_traces`."""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    class ClickHouseSpanExporter(SpanExporter):
        def __init__(self) -> None:
            self._client = _make_ch_client_for_otel()
            self._disabled = False

        def export(self, spans: Any) -> SpanExportResult:
            if not spans or self._disabled:
                return SpanExportResult.SUCCESS
            try:
                rows = [_span_to_row(s, service_name) for s in spans]
                self._client.insert(
                    "default.otel_traces",
                    data=[list(r.values()) for r in rows],
                    column_names=list(rows[0].keys()),
                )
                return SpanExportResult.SUCCESS
            except Exception as e:  # export fails soft: FAILURE to the SDK, never a raise
                if why := _unwritable(e):
                    log.log(
                        _disabled_level(),
                        "OTel span exporter: cannot write default.otel_traces: %s. "
                        "Self-disabling span export. To enable, deploy ClickStack "
                        "(or grant the user INSERT there) or set LOOM_OTEL_EXPORTER=otlp "
                        "+ LOOM_OTEL_EXPORTER_OTLP_ENDPOINT to ship spans elsewhere.",
                        why,
                    )
                    self._disabled = True
                    return SpanExportResult.SUCCESS
                log.exception(
                    "ClickHouseSpanExporter.export failed (%d spans dropped)",
                    len(spans),
                )
                return SpanExportResult.FAILURE

        def shutdown(self) -> None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — close at shutdown is best-effort
                pass

    return ClickHouseSpanExporter()


# Spans: the BatchSpanProcessor's schedule delay. Deliberately NOT tied to the
# metrics interval: a trace is what an operator opens first, and
# 3 s bounds the span-loss window before SIGTERM.
_SPAN_SCHEDULE_DELAY_MS = 3000


def _metric_reader_kwargs() -> dict[str, int]:
    """`PeriodicExportingMetricReader` cadence from settings.

    The export timeout is half the interval, capped at 10 s: the reader runs
    every gauge callback inside collect(), and the probes behind them are
    cached, so a collect that needs longer is stuck, not
    slow.
    """
    interval_ms = max(1, int(settings.otel_metrics_export_interval_s)) * 1000
    return {
        "export_interval_millis": interval_ms,
        "export_timeout_millis": max(500, min(interval_ms // 2, 10_000)),
    }


def _metric_preferred_temporality() -> dict[type, Any] | None:
    """DELTA for the counters and histograms (an observable counter too) when
    `otel_metrics_delta_temporality` is on, else None (the SDK's CUMULATIVE).

    With DELTA the SDK returns no data point for a series that saw no
    measurement in the interval, so idle series cost nothing; a reader must
    then sum rows instead of differencing them, so turning it on is a
    change every dashboard reading these tables has to make with it.
    """
    if not settings.otel_metrics_delta_temporality:
        return None
    try:
        from opentelemetry.sdk.metrics import Counter, Histogram, ObservableCounter, UpDownCounter
        from opentelemetry.sdk.metrics.export import AggregationTemporality
    except ImportError:  # pragma: no cover — SDK shape guard
        return None
    return {
        Counter: AggregationTemporality.DELTA,
        UpDownCounter: AggregationTemporality.DELTA,
        Histogram: AggregationTemporality.DELTA,
        ObservableCounter: AggregationTemporality.DELTA,
    }


class _GaugeChangeFilter:
    """Admit a gauge point when its value changed or the heartbeat lapsed.

    An observable gauge is re-read on every export whether or not it moved;
    the code-plane gauges (dozens of series each, one value per worker pass)
    reach millions of rows an hour with several processes exporting every 3 s. With a
    heartbeat of H seconds a flat series costs one row per H per process.
    `heartbeat_s <= 0` admits everything (the shipped default; see
    `otel_gauge_heartbeat_s`).
    """

    _MAX_SERIES = 50_000

    def __init__(self, heartbeat_s: float) -> None:
        self._heartbeat_ns = int(max(0.0, float(heartbeat_s)) * 1_000_000_000)
        self._last: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]] = {}

    def admit(self, name: str, attrs: dict[str, str], value: float, time_ns: int) -> bool:
        if self._heartbeat_ns <= 0:
            return True
        key = (name, tuple(sorted(attrs.items())))
        prev = self._last.get(key)
        if prev is not None and prev[0] == value and (time_ns - prev[1]) < self._heartbeat_ns:
            return False
        if len(self._last) >= self._MAX_SERIES and key not in self._last:
            self._last.clear()
        self._last[key] = (value, time_ns)
        return True


def _make_clickhouse_metric_exporter(service_name: str) -> Any:
    """Build a MetricExporter writing Sum → otel_metrics_sum, Histogram →
    otel_metrics_histogram and Gauge → otel_metrics_gauge.

    The gauges are the domain state gauges (`server/observability/metrics_gauges.py`).
    Anything not in these three shapes (Summary, ExponentialHistogram) is
    dropped; Loom emits none.
    """
    from datetime import datetime

    from opentelemetry.sdk.metrics.export import (
        Gauge,
        Histogram,
        MetricExporter,
        MetricExportResult,
        Sum,
    )

    def _ns_to_dt(ns: int) -> datetime:
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)

    class ClickHouseMetricExporter(MetricExporter):
        def __init__(self) -> None:
            # Temporality is the reader's contract with the
            # dashboards; the SDK reads it from here via the metric reader.
            super().__init__(preferred_temporality=_metric_preferred_temporality())
            self._client = _make_ch_client_for_otel()
            self._disabled = False
            self._gauge_filter = _GaugeChangeFilter(settings.otel_gauge_heartbeat_s)

        def export(
            self, metrics_data: Any, timeout_millis: float = 10_000, **kwargs: Any
        ) -> MetricExportResult:
            if self._disabled:
                return MetricExportResult.SUCCESS
            try:
                sum_rows: list[dict[str, Any]] = []
                hist_rows: list[dict[str, Any]] = []
                gauge_rows: list[dict[str, Any]] = []
                for rm in metrics_data.resource_metrics:
                    resource_attrs = _attrs_to_map(rm.resource.attributes)
                    svc = resource_attrs.get("service.name", service_name)
                    for sm in rm.scope_metrics:
                        scope_name = sm.scope.name if sm.scope else ""
                        scope_ver = sm.scope.version if sm.scope and sm.scope.version else ""
                        for metric in sm.metrics:
                            d = metric.data
                            if isinstance(d, Sum):
                                for dp in d.data_points:
                                    sum_rows.append(
                                        {
                                            "ResourceAttributes": resource_attrs,
                                            "ResourceSchemaUrl": "",
                                            "ScopeName": scope_name,
                                            "ScopeVersion": scope_ver,
                                            "ScopeAttributes": {},
                                            "ScopeDroppedAttrCount": 0,
                                            "ScopeSchemaUrl": "",
                                            "ServiceName": svc,
                                            "MetricName": metric.name,
                                            "MetricDescription": metric.description or "",
                                            "MetricUnit": metric.unit or "",
                                            "Attributes": _attrs_to_map(dp.attributes),
                                            "StartTimeUnix": _ns_to_dt(dp.start_time_unix_nano),
                                            "TimeUnix": _ns_to_dt(dp.time_unix_nano),
                                            "Value": float(dp.value),
                                            "Flags": 0,
                                            "Exemplars.FilteredAttributes": [],
                                            "Exemplars.TimeUnix": [],
                                            "Exemplars.Value": [],
                                            "Exemplars.SpanId": [],
                                            "Exemplars.TraceId": [],
                                            "AggregationTemporality": int(
                                                d.aggregation_temporality
                                            ),
                                            "IsMonotonic": bool(d.is_monotonic),
                                        }
                                    )
                            elif isinstance(d, Gauge):
                                # Gauges carry no temporality or monotonicity —
                                # each point is a level read at export time.
                                for dp in d.data_points:
                                    gauge_attrs = _attrs_to_map(dp.attributes)
                                    gauge_value = float(dp.value)
                                    if not self._gauge_filter.admit(
                                        metric.name, gauge_attrs, gauge_value, dp.time_unix_nano
                                    ):
                                        continue
                                    gauge_rows.append(
                                        {
                                            "ResourceAttributes": resource_attrs,
                                            "ResourceSchemaUrl": "",
                                            "ScopeName": scope_name,
                                            "ScopeVersion": scope_ver,
                                            "ScopeAttributes": {},
                                            "ScopeDroppedAttrCount": 0,
                                            "ScopeSchemaUrl": "",
                                            "ServiceName": svc,
                                            "MetricName": metric.name,
                                            "MetricDescription": metric.description or "",
                                            "MetricUnit": metric.unit or "",
                                            "Attributes": gauge_attrs,
                                            "StartTimeUnix": _ns_to_dt(
                                                dp.start_time_unix_nano or 0
                                            ),
                                            "TimeUnix": _ns_to_dt(dp.time_unix_nano),
                                            "Value": gauge_value,
                                            "Flags": 0,
                                            "Exemplars.FilteredAttributes": [],
                                            "Exemplars.TimeUnix": [],
                                            "Exemplars.Value": [],
                                            "Exemplars.SpanId": [],
                                            "Exemplars.TraceId": [],
                                        }
                                    )
                            elif isinstance(d, Histogram):
                                for hdp in d.data_points:
                                    hist_rows.append(
                                        {
                                            "ResourceAttributes": resource_attrs,
                                            "ResourceSchemaUrl": "",
                                            "ScopeName": scope_name,
                                            "ScopeVersion": scope_ver,
                                            "ScopeAttributes": {},
                                            "ScopeDroppedAttrCount": 0,
                                            "ScopeSchemaUrl": "",
                                            "ServiceName": svc,
                                            "MetricName": metric.name,
                                            "MetricDescription": metric.description or "",
                                            "MetricUnit": metric.unit or "",
                                            "Attributes": _attrs_to_map(hdp.attributes),
                                            "StartTimeUnix": _ns_to_dt(hdp.start_time_unix_nano),
                                            "TimeUnix": _ns_to_dt(hdp.time_unix_nano),
                                            "Count": int(hdp.count),
                                            "Sum": float(hdp.sum or 0.0),
                                            "BucketCounts": list(hdp.bucket_counts),
                                            "ExplicitBounds": list(hdp.explicit_bounds),
                                            "Exemplars.FilteredAttributes": [],
                                            "Exemplars.TimeUnix": [],
                                            "Exemplars.Value": [],
                                            "Exemplars.SpanId": [],
                                            "Exemplars.TraceId": [],
                                            "Flags": 0,
                                            "Min": (float(hdp.min) if hdp.min is not None else 0.0),
                                            "Max": (float(hdp.max) if hdp.max is not None else 0.0),
                                            "AggregationTemporality": int(
                                                d.aggregation_temporality
                                            ),
                                        }
                                    )
                if sum_rows:
                    self._client.insert(
                        "default.otel_metrics_sum",
                        data=[list(r.values()) for r in sum_rows],
                        column_names=list(sum_rows[0].keys()),
                    )
                if hist_rows:
                    self._client.insert(
                        "default.otel_metrics_histogram",
                        data=[list(r.values()) for r in hist_rows],
                        column_names=list(hist_rows[0].keys()),
                    )
                if gauge_rows:
                    self._client.insert(
                        "default.otel_metrics_gauge",
                        data=[list(r.values()) for r in gauge_rows],
                        column_names=list(gauge_rows[0].keys()),
                    )
                return MetricExportResult.SUCCESS
            except Exception as e:  # export fails soft: FAILURE to the SDK, never a raise
                if why := _unwritable(e):
                    log.log(
                        _disabled_level(),
                        "OTel metric exporter: cannot write default.otel_metrics_*: %s. "
                        "Self-disabling metric export.",
                        why,
                    )
                    self._disabled = True
                    return MetricExportResult.SUCCESS
                log.exception("ClickHouseMetricExporter.export failed")
                return MetricExportResult.FAILURE

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            return True

        def shutdown(self, timeout_millis: float = 30_000, **kwargs: Any) -> None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — close at shutdown is best-effort
                pass

    return ClickHouseMetricExporter()


def _make_clickhouse_log_exporter(service_name: str) -> Any:
    """Build a LogExporter that INSERTs into `default.otel_logs`.

    Logs land alongside traces; the OTel logging handler attaches the
    active span's trace_id + span_id, so HyperDX can click-through from
    a trace to its emitted logs.
    """
    from opentelemetry.sdk._logs import ReadableLogRecord
    from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult

    class ClickHouseLogExporter(LogRecordExporter):
        def __init__(self) -> None:
            self._client = _make_ch_client_for_otel()
            self._disabled = False

        def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
            if not batch or self._disabled:
                return LogRecordExportResult.SUCCESS
            try:
                rows = [_log_to_row(rec, service_name) for rec in batch]
                self._client.insert(
                    "default.otel_logs",
                    data=[list(r.values()) for r in rows],
                    column_names=list(rows[0].keys()),
                )
                return LogRecordExportResult.SUCCESS
            except Exception as e:  # export fails soft: FAILURE to the SDK, never a raise
                if why := _unwritable(e):
                    log.log(
                        _disabled_level(),
                        "OTel log exporter: cannot write default.otel_logs: %s. "
                        "Self-disabling log export.",
                        why,
                    )
                    self._disabled = True
                    return LogRecordExportResult.SUCCESS
                log.exception("ClickHouseLogExporter.export failed (%d logs dropped)", len(batch))
                return LogRecordExportResult.FAILURE

        def shutdown(self) -> None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — close at shutdown is best-effort
                pass

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

    return ClickHouseLogExporter()


# ---- Span → CH row -----------------------------------------------------

# OTel SpanKind enum (1=INTERNAL, 2=SERVER, 3=CLIENT, 4=PRODUCER, 5=CONSUMER).
_KIND_NAMES = {1: "Internal", 2: "Server", 3: "Client", 4: "Producer", 5: "Consumer"}
# Status code enum (0=UNSET, 1=OK, 2=ERROR).
_STATUS_NAMES = {0: "Unset", 1: "Ok", 2: "Error"}


def _stringify_attr(v: Any) -> str:
    """OTel attribute values are bool|int|float|str|sequence; CH wants Map<String,String>."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return ",".join(_stringify_attr(x) for x in v)
    return str(v)


def _attrs_to_map(attrs: Any) -> dict[str, str]:
    if not attrs:
        return {}
    if hasattr(attrs, "items"):
        return {k: _stringify_attr(v) for k, v in attrs.items()}
    return {}


def _span_to_row(span: Any, service_name: str) -> dict[str, Any]:
    """Convert a ReadableSpan to a `default.otel_traces` row dict.

    Column ordering matches the schema in
    `schemas/otel/clickstack_otel_tables.sql`. Nested columns are written
    as parallel arrays per CH's nested-column convention.
    """
    from datetime import datetime

    ctx = span.get_span_context()
    parent = span.parent
    parent_id_hex = format(parent.span_id, "016x") if parent else ""

    # OTel nanos-since-epoch → datetime; clickhouse-connect serializes
    # losslessly into DateTime64(9).
    ts = datetime.fromtimestamp(span.start_time / 1_000_000_000, tz=UTC)
    duration_ns = (span.end_time or span.start_time) - span.start_time

    resource_attrs = _attrs_to_map(span.resource.attributes)
    span_attrs = _attrs_to_map(span.attributes)
    scope = span.instrumentation_scope

    # Events → parallel arrays
    ev_ts: list[datetime] = []
    ev_names: list[str] = []
    ev_attrs: list[dict[str, str]] = []
    for e in span.events or []:
        ev_ts.append(datetime.fromtimestamp(e.timestamp / 1_000_000_000, tz=UTC))
        ev_names.append(e.name)
        ev_attrs.append(_attrs_to_map(e.attributes))

    # Links → parallel arrays
    ln_trace: list[str] = []
    ln_span: list[str] = []
    ln_state: list[str] = []
    ln_attrs: list[dict[str, str]] = []
    for ln in span.links or []:
        c = ln.context
        ln_trace.append(format(c.trace_id, "032x"))
        ln_span.append(format(c.span_id, "016x"))
        ln_state.append(str(c.trace_state) if c.trace_state else "")
        ln_attrs.append(_attrs_to_map(ln.attributes))

    status_code = _STATUS_NAMES.get(int(span.status.status_code.value), "Unset")
    status_msg = span.status.description or ""

    return {
        "Timestamp": ts,
        "TraceId": format(ctx.trace_id, "032x"),
        "SpanId": format(ctx.span_id, "016x"),
        "ParentSpanId": parent_id_hex,
        "TraceState": str(ctx.trace_state) if ctx.trace_state else "",
        "SpanName": span.name,
        "SpanKind": _KIND_NAMES.get(int(span.kind.value), "Internal"),
        "ServiceName": resource_attrs.get("service.name", service_name),
        "ResourceAttributes": resource_attrs,
        "ScopeName": scope.name if scope else "",
        "ScopeVersion": scope.version if scope and scope.version else "",
        "SpanAttributes": span_attrs,
        "Duration": int(duration_ns),
        "StatusCode": status_code,
        "StatusMessage": status_msg,
        "Events.Timestamp": ev_ts,
        "Events.Name": ev_names,
        "Events.Attributes": ev_attrs,
        "Links.TraceId": ln_trace,
        "Links.SpanId": ln_span,
        "Links.TraceState": ln_state,
        "Links.Attributes": ln_attrs,
    }


# ---- LogRecord → CH row ------------------------------------------------

# Python's `logging` levels → OTel severity. OTel uses a 1-24 scale where
# higher = more severe; the canonical mapping per the spec.
_SEVERITY_TEXT = {
    "TRACE": ("TRACE", 1),
    "DEBUG": ("DEBUG", 5),
    "INFO": ("INFO", 9),
    "WARNING": ("WARN", 13),
    "WARN": ("WARN", 13),
    "ERROR": ("ERROR", 17),
    "CRITICAL": ("FATAL", 21),
    "FATAL": ("FATAL", 21),
}


def _log_to_row(record: Any, service_name: str) -> dict[str, Any]:
    """Convert an OTel LogData (record + resource) to a `default.otel_logs` row."""
    from datetime import datetime

    lr = record.log_record
    res = record.instrumentation_scope

    ts_ns = lr.timestamp or lr.observed_timestamp or 0
    ts = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=UTC) if ts_ns else datetime.now(UTC)

    sev_text, sev_num = _SEVERITY_TEXT.get(
        (lr.severity_text or "").upper(),
        ("INFO", int(lr.severity_number.value) if lr.severity_number else 9),
    )

    body = lr.body if isinstance(lr.body, str) else str(lr.body or "")
    log_attrs = _attrs_to_map(lr.attributes)
    resource_attrs = _attrs_to_map(record.resource.attributes if record.resource else {})

    return {
        "Timestamp": ts,
        "TraceId": format(lr.trace_id, "032x") if lr.trace_id else "",
        "SpanId": format(lr.span_id, "016x") if lr.span_id else "",
        "TraceFlags": int(lr.trace_flags) if lr.trace_flags else 0,
        "SeverityText": sev_text,
        "SeverityNumber": sev_num,
        "ServiceName": resource_attrs.get("service.name", service_name),
        "Body": body,
        "ResourceSchemaUrl": "",
        "ResourceAttributes": resource_attrs,
        "ScopeSchemaUrl": "",
        "ScopeName": res.name if res else "",
        "ScopeVersion": res.version if res and res.version else "",
        "ScopeAttributes": {},
        "LogAttributes": log_attrs,
    }
