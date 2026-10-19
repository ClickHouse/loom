"""Metric export cadence and shape.

Metrics export once a minute per process by default (a 3 s cadence writes
millions of rows an hour into default.otel_metrics_* for series read at
minute resolution); spans keep their own 3 s batch delay; delta temporality and the
gauge change filter exist as settings, default off, and do what they say when
on. The exporter is driven through the real SDK (MeterProvider + a periodic
reader whose interval never fires; `collect()` is called by hand) against a
stub ClickHouse client, so the rows asserted are the rows a deployment would get.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.observability import otel_exporters
from server.settings import Settings, settings


class _StubClient:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[list[Any]], list[str]]] = []
        self.closed = False

    def insert(self, table: str, data: list[list[Any]], column_names: list[str]) -> None:
        self.inserts.append((table, data, column_names))

    def close(self) -> None:
        self.closed = True

    def rows(self, table: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for t, data, cols in self.inserts:
            if t == table:
                out.extend(dict(zip(cols, r, strict=True)) for r in data)
        return out


def _provider(monkeypatch: pytest.MonkeyPatch, client: _StubClient):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: client)
    exporter = otel_exporters._make_clickhouse_metric_exporter("loom-test")
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10**9)
    provider = MeterProvider(metric_readers=[reader])
    return provider, reader, exporter


def test_default_cadence_is_one_minute_and_spans_keep_three_seconds() -> None:
    assert Settings.model_fields["otel_metrics_export_interval_s"].default == 60
    assert Settings.model_fields["otel_metrics_delta_temporality"].default is False
    assert Settings.model_fields["otel_gauge_heartbeat_s"].default == 0
    # The span processor's delay is not the metrics interval.
    assert otel_exporters._SPAN_SCHEDULE_DELAY_MS == 3000


def test_reader_kwargs_follow_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otel_metrics_export_interval_s", 60)
    kw = otel_exporters._metric_reader_kwargs()
    assert kw == {"export_interval_millis": 60_000, "export_timeout_millis": 10_000}
    monkeypatch.setattr(settings, "otel_metrics_export_interval_s", 3)
    kw = otel_exporters._metric_reader_kwargs()
    assert kw["export_interval_millis"] == 3_000 and kw["export_timeout_millis"] == 1_500
    monkeypatch.setattr(settings, "otel_metrics_export_interval_s", 0)
    assert otel_exporters._metric_reader_kwargs()["export_interval_millis"] == 1_000


def test_cumulative_by_default_writes_idle_series_every_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "otel_metrics_delta_temporality", False)
    monkeypatch.setattr(settings, "otel_gauge_heartbeat_s", 0)
    client = _StubClient()
    provider, reader, _ = _provider(monkeypatch, client)
    try:
        meter = provider.get_meter("t")
        c = meter.create_counter("loom.requests")
        h = meter.create_histogram("loom.request.duration")
        c.add(2, {"loom.operation": "memory.search"})
        h.record(12.5, {"loom.operation": "memory.search"})
        reader.collect()
        reader.collect()  # no new observations
        # Snapshot before shutdown: the SDK flushes one more collection there.
        sums = client.rows("default.otel_metrics_sum")
        hists = client.rows("default.otel_metrics_histogram")
    finally:
        provider.shutdown()
    assert len(sums) == 2 and len(hists) == 2, "cumulative: an idle series is re-written"
    assert {r["AggregationTemporality"] for r in sums + hists} == {2}
    assert [r["Value"] for r in sums] == [2.0, 2.0]


def test_delta_temporality_skips_idle_series(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otel_metrics_delta_temporality", True)
    monkeypatch.setattr(settings, "otel_gauge_heartbeat_s", 0)
    client = _StubClient()
    provider, reader, exporter = _provider(monkeypatch, client)
    try:
        from opentelemetry.sdk.metrics import Counter, Histogram
        from opentelemetry.sdk.metrics.export import AggregationTemporality

        assert exporter._preferred_temporality[Counter] is AggregationTemporality.DELTA
        assert exporter._preferred_temporality[Histogram] is AggregationTemporality.DELTA
        meter = provider.get_meter("t")
        c = meter.create_counter("loom.requests")
        h = meter.create_histogram("loom.request.duration")
        c.add(2, {"loom.operation": "memory.search"})
        h.record(12.5, {"loom.operation": "memory.search"})
        reader.collect()
        reader.collect()  # idle interval: nothing to write
        c.add(3, {"loom.operation": "memory.search"})
        reader.collect()
        sums = client.rows("default.otel_metrics_sum")
        hists = client.rows("default.otel_metrics_histogram")
    finally:
        provider.shutdown()
    assert [r["Value"] for r in sums] == [2.0, 3.0], "delta: each row is the interval's increase"
    assert len(hists) == 1
    assert {r["AggregationTemporality"] for r in sums + hists} == {1}


def test_gauge_change_filter_admits_changes_and_heartbeats() -> None:
    f = otel_exporters._GaugeChangeFilter(300)
    s = 1_000_000_000
    assert f.admit("g", {"ns": "a"}, 5.0, 0 * s) is True
    assert f.admit("g", {"ns": "a"}, 5.0, 60 * s) is False, "unchanged inside the heartbeat"
    assert f.admit("g", {"ns": "b"}, 5.0, 60 * s) is True, "another series is another key"
    assert f.admit("g", {"ns": "a"}, 6.0, 120 * s) is True, "a change is written at once"
    assert f.admit("g", {"ns": "a"}, 6.0, 419 * s) is False
    assert f.admit("g", {"ns": "a"}, 6.0, 420 * s) is True, "heartbeat lapsed"
    off = otel_exporters._GaugeChangeFilter(0)
    assert all(off.admit("g", {}, 1.0, t * s) for t in range(5)), "0 = every export"


def test_gauge_heartbeat_through_the_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otel_metrics_delta_temporality", False)
    monkeypatch.setattr(settings, "otel_gauge_heartbeat_s", 300)
    client = _StubClient()
    provider, reader, _ = _provider(monkeypatch, client)
    level = {"v": 7}
    try:
        from opentelemetry.metrics import Observation

        meter = provider.get_meter("t")
        meter.create_observable_gauge(
            "loom.embed.pending", callbacks=[lambda _o: [Observation(level["v"])]]
        )
        reader.collect()
        reader.collect()  # same level, well inside 300 s: not written
        level["v"] = 8
        reader.collect()
        gauges = client.rows("default.otel_metrics_gauge")
    finally:
        provider.shutdown()
    assert [r["Value"] for r in gauges] == [7.0, 8.0]
    assert client.closed is True


def test_gauges_written_every_export_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otel_metrics_delta_temporality", False)
    monkeypatch.setattr(settings, "otel_gauge_heartbeat_s", 0)
    client = _StubClient()
    provider, reader, _ = _provider(monkeypatch, client)
    try:
        from opentelemetry.metrics import Observation

        meter = provider.get_meter("t")
        meter.create_observable_gauge("loom.embed.pending", callbacks=[lambda _o: [Observation(7)]])
        reader.collect()
        reader.collect()
        gauges = client.rows("default.otel_metrics_gauge")
    finally:
        provider.shutdown()
    assert [r["Value"] for r in gauges] == [7.0, 7.0]


def test_delta_temporality_turns_an_observable_total_into_increases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """loom.ledger.dropped reports a running total; with delta on, its rows are
    summed like every other counter's, so they must be the increases."""
    monkeypatch.setattr(settings, "otel_metrics_delta_temporality", True)
    monkeypatch.setattr(settings, "otel_gauge_heartbeat_s", 0)
    client = _StubClient()
    provider, reader, _ = _provider(monkeypatch, client)
    totals = iter([3, 8, 8])  # the last for the collection shutdown runs
    try:
        from opentelemetry.metrics import Observation

        meter = provider.get_meter("t")
        meter.create_observable_counter(
            "loom.ledger.dropped",
            callbacks=[lambda _o: [Observation(next(totals), {"reason": "queue_full"})]],
        )
        reader.collect()
        reader.collect()
        sums = client.rows("default.otel_metrics_sum")
    finally:
        provider.shutdown()
    assert [r["Value"] for r in sums] == [3.0, 5.0]
    assert {r["AggregationTemporality"] for r in sums} == {1}
