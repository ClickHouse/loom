"""Private request outcomes never enter public telemetry queues or histograms."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse

from server.core import diagnostics, request_work
from server.observability import otel, otel_langfuse
from server.observability.otel_privacy import asgi_meter_provider, public_span_processor
from server.transport.request_scope import HTTPApplication


@pytest.fixture(autouse=True)
def isolated_work():
    token = request_work._current.set(None)
    try:
        yield
    finally:
        request_work._current.reset(token)


@pytest.fixture(autouse=True)
def instrumentor_background_state():
    # Uninstrumenting a local app also removes the process-wide background hook.
    missing = object()
    original_call = BackgroundTask.__call__
    original_hook = vars(BackgroundTask).get("_otel_original_call", missing)
    try:
        yield
    finally:
        BackgroundTask.__call__ = original_call
        if original_hook is missing:
            if "_otel_original_call" in vars(BackgroundTask):
                delattr(BackgroundTask, "_otel_original_call")
        else:
            BackgroundTask._otel_original_call = original_hook


@pytest.fixture
def local_traces():
    provider = TracerProvider(sampler=ALWAYS_ON, resource=Resource({}), shutdown_on_exit=False)
    try:
        exporter = InMemorySpanExporter()
        batch = BatchSpanProcessor(exporter)
        try:
            provider.add_span_processor(public_span_processor(batch))
        except BaseException:
            batch.shutdown()
            raise
        yield provider, exporter
    finally:
        provider.shutdown()


def mark():
    request_work.begin()
    assert request_work.mark_mirrored()


def test_owner_survives_private_scope_without_hiding_accounting():
    assert diagnostics.public_diagnostics()
    with request_work.http_owner({}):
        bag = request_work.begin()
        with diagnostics.private_work():
            assert request_work.mark_mirrored()
            assert not diagnostics.public_diagnostics()
            assert request_work.current() is None
        assert not diagnostics.codes_only()
        assert not diagnostics.public_diagnostics()
        assert request_work.current() is bag
        request_work.begin()
        assert not request_work.mirror_marked()
        assert not diagnostics.public_diagnostics()
    assert diagnostics.public_diagnostics()


def test_unavailable_owner_decision_drops_public_sinks(monkeypatch):
    def unavailable():
        raise RuntimeError("private decision unavailable")

    monkeypatch.setattr(request_work, "http_mirror_marked", unavailable)
    emit = Mock()
    diagnostics.public_only(emit)("hidden")
    record = logging.LogRecord("example", logging.ERROR, "", 0, "hidden", (), None)
    assert not diagnostics.public_diagnostics()
    assert not diagnostics.public_log(record)
    emit.assert_not_called()


@pytest.mark.parametrize("transport", ["http", "mcp"])
@pytest.mark.parametrize("success", [True, False])
def test_operation_metrics_keep_ordinary_values_and_drop_marked(monkeypatch, transport, success):
    counter, duration = Mock(), Mock()
    monkeypatch.setattr(otel, "_request_counter", counter)
    monkeypatch.setattr(otel, "_request_duration", duration)
    values = dict(
        namespace="example",
        success=success,
        duration_ms=17.5,
        transport=transport,
        error_code="" if success else "UNAVAILABLE",
    )
    with request_work.http_owner({}):
        mark()
        otel.record_op("memory.search", **values)
    counter.add.assert_not_called()
    duration.record.assert_not_called()
    otel.record_op("memory.search", **values)
    attrs = {
        "loom.operation": "memory.search",
        "loom.namespace": "example",
        "loom.transport": transport,
        "success": "true" if success else "false",
    }
    counter.add.assert_called_once_with(
        1, attributes={**attrs, "loom.error_code": values["error_code"]}
    )
    duration.record.assert_called_once_with(17.5, attributes=attrs)


def test_batch_export_after_owner_reset_contains_only_ordinary_spans(local_traces):
    provider, exporter = local_traces
    tracer = provider.get_tracer("example")
    with request_work.http_owner({}):
        span = tracer.start_span("private-canary", start_time=1)
        mark()
        span.end(end_time=2)
    ordinary = tracer.start_span("ordinary", start_time=3)
    ordinary.set_attribute("example.value", "retained")
    ordinary.end(end_time=4)
    assert provider.force_flush()
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["ordinary"]
    assert spans[0].attributes["example.value"] == "retained"
    assert (spans[0].start_time, spans[0].end_time) == (3, 4)


def test_span_processor_preserves_public_lifecycle():
    inner = Mock()
    inner.force_flush.return_value = True
    processor = public_span_processor(inner)
    span, parent = object(), object()
    processor.on_start(span, parent)
    processor.on_end(span)
    assert processor.force_flush(123)
    processor.shutdown()
    inner.on_start.assert_called_once_with(span, parent)
    inner.on_end.assert_called_once_with(span)
    inner.force_flush.assert_called_once_with(123)
    inner.shutdown.assert_called_once_with()


def test_langfuse_does_not_inspect_count_or_export_marked_spans(monkeypatch):
    inner, span_filter = Mock(), Mock()
    span_filter.keep.return_value = True
    monkeypatch.setattr(otel_langfuse.genai, "stamp_identity", Mock())
    processor = otel_langfuse._langfuse_processor_class()(inner, span_filter)
    span = object()
    with request_work.http_owner({}):
        mark()
        processor.on_start(span)
        processor._on_ending(span)
        processor.on_end(span)
    assert (processor.forwarded, processor.dropped, processor.errors) == (0, 0, 0)
    assert inner.mock_calls == []
    span_filter.keep.assert_not_called()
    otel_langfuse.genai.stamp_identity.assert_not_called()
    processor.on_start(span)
    processor.on_end(span)
    assert (processor.forwarded, processor.dropped, processor.errors) == (1, 0, 0)
    inner.on_start.assert_called_once_with(span, None)
    inner.on_end.assert_called_once_with(span)
    span_filter.keep.assert_called_once_with(span)


def test_meter_preserves_identity_histogram_arguments_and_records():
    inner = Mock()
    wrapper = asgi_meter_provider(inner)
    attrs = {"example.scope": "value"}
    meter = wrapper.get_meter("example", "1", "https://example.com/schema", attrs)
    inner.get_meter.assert_called_once_with("example", "1", "https://example.com/schema", attrs)
    histogram = meter.create_histogram(
        "duration",
        unit="s",
        description="Elapsed time",
        explicit_bucket_boundaries_advisory=(0.1, 1.0),
    )
    raw = inner.get_meter.return_value.create_histogram.return_value
    inner.get_meter.return_value.create_histogram.assert_called_once_with(
        "duration",
        unit="s",
        description="Elapsed time",
        explicit_bucket_boundaries_advisory=(0.1, 1.0),
    )
    context = object()
    with request_work.http_owner({}):
        mark()
        histogram.record(9, attrs, context)
    raw.record.assert_not_called()
    histogram.record(1.5, attrs, context)
    raw.record.assert_called_once_with(1.5, attrs, context)


@pytest.mark.parametrize(
    "method",
    [
        "create_counter",
        "create_up_down_counter",
        "create_observable_counter",
        "create_observable_up_down_counter",
        "create_gauge",
        "create_observable_gauge",
    ],
)
def test_other_meter_instruments_delegate_without_changing_participation(method):
    inner = Mock()
    meter = asgi_meter_provider(inner).get_meter("example")
    create = getattr(inner.get_meter.return_value, method)
    with request_work.http_owner({}):
        mark()
        instrument = getattr(meter, method)("active", unit="1", description="Active")
    assert instrument is create.return_value
    create.assert_called_once_with("active", unit="1", description="Active")


@pytest.mark.parametrize("raises", [False, True])
async def test_real_instrumentor_finalizers_drop_marked_and_keep_ordinary(raises, local_traces):
    histograms, active = [], []

    class Histogram:
        def __init__(self, name):
            self.name = name

        def record(self, amount, attributes=None, context=None):
            histograms.append((self.name, amount, attributes))

    class Counter:
        def add(self, amount, *args, **kwargs):
            active.append(amount)

    class Meter:
        def create_histogram(self, name, *args, **kwargs):
            return Histogram(name)

        def create_up_down_counter(self, name, *args, **kwargs):
            return Counter()

    provider, exporter = local_traces
    app = HTTPApplication()

    async def endpoint(scope, receive, send):
        if scope["path"] == "/marked":
            mark()
            if raises:
                raise RuntimeError("private-exception-canary")
        await PlainTextResponse("ready")(scope, receive, send)

    app.mount("/", endpoint)
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            meter_provider=asgi_meter_provider(SimpleNamespace(get_meter=lambda *args: Meter())),
            excluded_urls="",
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/marked")
            assert response.status_code == (500 if raises else 200)
            assert not request_work.http_mirror_marked()
            assert provider.force_flush()
            assert exporter.get_finished_spans() == ()
            assert histograms == []
            assert active and sum(active) == 0
            ordinary = await client.get("/ordinary")
            assert ordinary.status_code == 200 and ordinary.text == "ready"
        assert provider.force_flush()
        spans = exporter.get_finished_spans()
        assert spans and histograms and sum(active) == 0
        assert all("private-exception-canary" not in str(span.events) for span in spans)
        assert all("/marked" not in str(dict(span.attributes)) for span in spans)
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
