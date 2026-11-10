"""OpenTelemetry SDK initialization for Loom.

Ships spans/logs/metrics into ClickStack's `otel_*` tables (in LOOM_CLICKSTACK_DATABASE,
`default` when unset) in two
exporter modes:

  - **Direct CH** (default, `LOOM_OTEL_EXPORTER=ch`): batch and INSERT
    straight into those tables via Loom's ClickHouse client — no
    collector. The schema matches what ClickStack's own collector writes,
    so the HyperDX UI renders them natively.
  - **OTLP/HTTP** (`LOOM_OTEL_EXPORTER=otlp`): ship to an external
    collector at `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT`.

No-op when the OTLP endpoint is empty AND `LOOM_OTEL_EXPORTER` is not `ch`:
`init_otel()` returns None and the request path emits no spans.

This module builds the providers and holds the request-path helpers. The
ClickHouse exporters live in `otel_exporters`, the Langfuse span processor in
`otel_langfuse`, and the state gauge probes in `otel_probes` (reading through
`otel_probe_client`).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from typing import Any

from server.core.ch_names import otel_table
from server.observability.otel_exporters import (
    _SPAN_SCHEDULE_DELAY_MS,
    _make_clickhouse_log_exporter,
    _make_clickhouse_metric_exporter,
    _make_clickhouse_span_exporter,
    _metric_preferred_temporality,
    _metric_reader_kwargs,
    _traces_backend_available,
)
from server.observability.otel_langfuse import _add_langfuse_processor
from server.observability.otel_probes import _state_gauge_probes
from server.settings import settings

log = logging.getLogger(__name__)

_initialized = False
_tracer_provider: Any = None
_logger_provider: Any = None
_log_handler: Any = None
_meter_provider: Any = None
_request_counter: Any = None
_request_duration: Any = None
_langfuse_processor: Any = None


def _parse_headers(raw: str) -> dict[str, str]:
    """Parse comma-separated K=V pairs into a dict. Tolerates whitespace."""
    out: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, _, v = item.partition("=")
        out[k.strip()] = v.strip()
    return out


# The `service.namespace` resource value on every Loom span, log and metric
# point: the deployment, per OTel semconv; the tenant rides `loom.namespace`.
SERVICE_NAMESPACE = "loom"

# Sign-in URLs carry codes and state in their query strings; whatever the
# setting says, they never go into a trace.
_SIGN_IN_URLS = "/oauth/"


def _fastapi_excluded_urls() -> str:
    """The instrumentor's `excluded_urls`: comma-separated
    regexes matched against the request URL; a match opens no server span, so
    the `http send` / `http receive` children never exist either. Default
    `/v1/health`: the healthcheck poll writes a span every few seconds that
    nobody reads. The sign-in routes are always added."""
    raw = str(settings.otel_excluded_urls).strip()
    return f"{raw},{_SIGN_IN_URLS}" if raw else _SIGN_IN_URLS


def _instrument_fastapi(app: Any) -> None:
    """One parent `POST /v1/<op>` span per request, minus the excluded paths.
    Never raises: telemetry must not fail a boot."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls=_fastapi_excluded_urls())
    except Exception as e:  # noqa: BLE001 — instrumentation is optional; boot continues without it
        log.warning("FastAPIInstrumentor.instrument_app failed: %s", e)


def init_otel(app: Any | None = None, *, enable_metrics: bool = True) -> Any:
    """Initialize OTel SDK + instrument FastAPI + httpx. Idempotent.

    Returns the TracerProvider (so callers can flush on shutdown).
    Returns None when disabled.

    `enable_metrics=False` is the CLI shape (`loom code ...`): spans and logs
    for a one-shot process, but no MeterProvider — the state gauges would
    otherwise be exported a second time from the CLI process with the same
    values, and a scip-full run lasts long enough for that to double every
    `loom.code.*` panel while it runs.
    """
    global _initialized, _tracer_provider
    if _initialized:
        return _tracer_provider

    # Guard: only a real server process may ship telemetry.
    #
    # `init_otel(app)` is called at MODULE level in server/main.py, so *any*
    # import of that module starts an exporter — including `pytest` collecting a
    # test that imports the app, and short-lived CLI invocations. With
    # LOOM_CH_HOST pointed at production that means test runs write spans, logs
    # and metrics into the production observability tables: ERROR rows such as
    # "No real embedder configured" or "no CH in unit test" land in
    # ClickStack's otel_logs, indistinguishable from a real incident on any
    # dashboard.
    #
    # pytest is detected rather than configured because the failure is silent
    # and the person adding a test will not think to opt out.
    if not settings.otel_enabled:
        log.info("OTel disabled via LOOM_OTEL_ENABLED=false")
        _initialized = True
        return None
    if "pytest" in sys.modules:
        log.debug("OTel suppressed: running under pytest")
        _initialized = True
        return None

    exporter_mode, endpoint = _exporter_mode()
    if exporter_mode is None:
        _initialized = True
        return None
    started = _start_tracing(exporter_mode, endpoint)
    if started is None:
        _initialized = True
        return None
    provider, resource, log_exporter = started
    _tracer_provider = provider

    # Metrics go where the spans go: straight into ClickHouse, or to the
    # OTLP endpoint (a collector in front of ClickStack, say).
    if enable_metrics:
        _init_metrics(resource, exporter_mode, endpoint)

    # Attach a LoggingHandler to the root logger: it stamps the active
    # span's trace_id/span_id on each record, so logs emitted inside a
    # request correlate with their parent trace in HyperDX.
    if log_exporter is not None:
        _attach_log_handler(resource, log_exporter)

    _instrument_http(app, exporter_mode, endpoint)

    _initialized = True
    return provider


def _exporter_mode() -> tuple[str | None, str]:
    """(`ch` | `otlp`, the OTLP endpoint); the mode is None when
    `LOOM_OTEL_EXPORTER` names neither."""
    exporter_mode = settings.otel_exporter.strip().lower()
    endpoint = settings.otel_exporter_otlp_endpoint
    # Resolve mode: explicit `ch` or `otlp`, else infer from endpoint.
    if not exporter_mode:
        exporter_mode = "ch" if not endpoint else "otlp"
    if exporter_mode not in ("ch", "otlp"):
        log.warning("invalid LOOM_OTEL_EXPORTER=%r; OTel disabled", exporter_mode)
        return None, endpoint
    return exporter_mode, endpoint


def _start_tracing(exporter_mode: str, endpoint: str) -> tuple[Any, Any, Any] | None:
    """Build the resource, the exporters and the TracerProvider (ClickHouse
    processor, then Langfuse) and install it. Returns (provider, resource,
    log exporter or None), or None when OTel is unavailable."""
    global _langfuse_processor
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        log.warning("opentelemetry packages not installed (%s) — OTel disabled", e)
        return None

    from server.observability.instance import resource_attributes as _instance_attrs

    # `service.instance.id` is hostname:pid (`$HOSTNAME` alone is set only
    # inside a container), plus loom.build_commit and the launcher's
    # loom.instance_label.
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.namespace": SERVICE_NAMESPACE,
            "deployment.environment.name": settings.otel_environment,
            **_instance_attrs(),
        }
    )

    if exporter_mode == "ch":
        try:
            span_exporter = _make_clickhouse_span_exporter(settings.otel_service_name)
            log_exporter = _make_clickhouse_log_exporter(settings.otel_service_name)
        except Exception as e:  # noqa: BLE001 — OTel is optional; disable on failure
            log.warning("ClickHouse OTel exporter init failed (%s); OTel disabled", e)
            return None
        log.info(
            "OTel SDK initialized — writing spans+logs directly to %s/%s",
            settings.ch_host,
            otel_table("otel_{traces,logs}"),
        )
    else:  # otlp
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        headers = _parse_headers(settings.otel_exporter_otlp_headers)
        span_exporter = OTLPSpanExporter(
            endpoint=endpoint.rstrip("/") + "/v1/traces",
            headers=headers if headers else None,
        )
        log_exporter = None  # OTLP log exporter — wire when needed
        log.info("OTel SDK initialized — exporting via OTLP/HTTP to %s", endpoint)

    provider = TracerProvider(resource=resource)
    # 3000ms (vs the 5000ms default) bounds the span-loss window before
    # SIGTERM while still batching enough per export to be efficient.
    provider.add_span_processor(
        BatchSpanProcessor(
            span_exporter,
            schedule_delay_millis=_SPAN_SCHEDULE_DELAY_MS,
        )
    )
    # Second, independent processor. Deliberately after the ClickHouse one and
    # deliberately not an `otel_exporter` mode — see `_LangfuseSpanProcessor`.
    _langfuse_processor = _add_langfuse_processor(provider)

    trace.set_tracer_provider(provider)
    return provider, resource, log_exporter


def _make_metric_exporter(exporter_mode: str, endpoint: str) -> Any:
    """The ClickHouse metric exporter, or OTLP/HTTP to `endpoint`'s
    `/v1/metrics` with the same temporality."""
    if exporter_mode == "ch":
        return _make_clickhouse_metric_exporter(settings.otel_service_name)
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    headers = _parse_headers(settings.otel_exporter_otlp_headers)
    return OTLPMetricExporter(
        endpoint=endpoint.rstrip("/") + "/v1/metrics",
        headers=headers if headers else None,
        preferred_temporality=_metric_preferred_temporality(),
    )


def _init_metrics(resource: Any, exporter_mode: str, endpoint: str) -> None:
    """The MeterProvider, the per-op request instruments, the domain metrics and
    the state gauges. Never raises: metrics must not stop startup."""
    global _meter_provider, _request_counter, _request_duration
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        metric_exporter = _make_metric_exporter(exporter_mode, endpoint)
        # One export a minute per process: a counter is read at minute
        # resolution, and the 3 s trace cadence would cost millions of rows
        # and hundreds of parts an hour per table.
        reader = PeriodicExportingMetricReader(metric_exporter, **_metric_reader_kwargs())
        _meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[reader],
            views=_quality_histogram_views(),
        )
        metrics.set_meter_provider(_meter_provider)
        # The scope name stays "loom.handlers": dashboards and alerts filter on it.
        meter = metrics.get_meter("loom.handlers")
        _request_counter = meter.create_counter(
            name="loom.requests",
            description="Count of Loom operations processed",
            unit="1",
        )
        _request_duration = meter.create_histogram(
            name="loom.request.duration",
            description="Wall-clock duration of Loom operations",
            unit="ms",
        )
        # Domain metrics (retrieval quality, lifecycle) + state gauges.
        # Kept in separate modules so cardinality rules live in one place;
        # see server/observability/metrics.py for why.
        try:
            from server.observability import metrics as _domain
            from server.observability import metrics_gauges as _gauges

            _domain.init_domain_metrics(meter)
            _gauges.register_state_gauges(meter, **_state_gauge_probes())
        except Exception as e:  # noqa: BLE001 — domain metrics must not stop OTel
            log.warning("domain metric wiring failed: %s", e)

        # The gauge heartbeat is the ClickHouse exporter's filter; OTLP has none.
        heartbeat = (
            f", gauge heartbeat {settings.otel_gauge_heartbeat_s}s" if exporter_mode == "ch" else ""
        )
        log.info(
            "OTel metrics initialized — emitting every %ds (%s temporality%s) to %s",
            settings.otel_metrics_export_interval_s,
            "delta" if settings.otel_metrics_delta_temporality else "cumulative",
            heartbeat,
            otel_table("otel_metrics_{sum,histogram,gauge}") if exporter_mode == "ch" else endpoint,
        )
    except Exception as e:  # noqa: BLE001 — metrics must not stop startup
        log.warning("OTel metrics init failed: %s", e)


def _attach_log_handler(resource: Any, log_exporter: Any) -> None:
    """A LoggerProvider on `log_exporter` and its LoggingHandler on the root
    logger. Never raises: log export must not stop startup."""
    global _logger_provider, _log_handler
    try:
        import logging as stdlogging

        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        set_logger_provider(logger_provider)

        # Attach LoggingHandler to the root logger so everything is captured.
        handler = LoggingHandler(level=stdlogging.INFO, logger_provider=logger_provider)
        stdlogging.getLogger().addHandler(handler)
        _logger_provider = logger_provider
        _log_handler = handler
        log.info("OTel logs handler attached to root logger")
    except Exception as e:  # noqa: BLE001 — log export must not stop startup
        log.warning("OTel log handler init failed: %s", e)


def _instrument_http(app: Any | None, exporter_mode: str, endpoint: str) -> None:
    """FastAPI server spans (when a traces backend exists) and httpx client spans."""
    # Auto-instrument FastAPI: one parent `POST /v1/<op>` span per request.
    # loom.* attributes are attached in `_make_endpoint`. Skipped when no traces
    # backend is reachable — instrumenting then is pointless and crashes the CORS
    # preflight on CI's OTel-asgi version (see _traces_backend_available).
    if app is not None and _traces_backend_available(exporter_mode, endpoint):
        _instrument_fastapi(app)
    elif app is not None:
        log.info(
            "OTel: no traces backend (no %s / OTLP endpoint) — "
            "skipping FastAPI auto-instrumentation",
            otel_table("otel_traces"),
        )

    # Auto-instrument outbound httpx calls as child spans of the request.
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001 — httpx spans are optional
        log.warning("HTTPXClientInstrumentor failed: %s", e)


def shutdown() -> None:
    """Flush pending spans + logs + metrics before process exit. Called from lifespan."""
    global _tracer_provider, _logger_provider, _meter_provider
    if _meter_provider is not None:
        try:
            _meter_provider.shutdown()
        except Exception as e:  # noqa: BLE001 — a failed flush must not block exit
            log.warning("OTel meter shutdown failed: %s", e)
        finally:
            _meter_provider = None
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
            log.info("OTel tracer provider shut down (pending spans flushed)")
        except Exception as e:  # noqa: BLE001 — a failed flush must not block exit
            log.warning("OTel tracer shutdown failed: %s", e)
        finally:
            _tracer_provider = None
    if _logger_provider is not None:
        global _log_handler
        # Detach first: anything logged after the provider is gone (aiohttp's
        # connector __del__ at interpreter teardown, for one) would otherwise
        # route into a shut-down exporter and die with "sys.meta_path is
        # None" — pure noise on every CLI exit.
        if _log_handler is not None:
            try:
                import logging as stdlogging

                stdlogging.getLogger().removeHandler(_log_handler)
            except Exception:  # noqa: BLE001 — a failed detach must not block exit
                pass
            _log_handler = None
        try:
            _logger_provider.shutdown()
        except Exception as e:  # noqa: BLE001 — a failed flush must not block exit
            log.warning("OTel logger shutdown failed: %s", e)
        finally:
            _logger_provider = None


def langfuse_processor_status() -> dict[str, Any]:
    """Health of the Langfuse span processor, for `/v1/health`.

    Exists because containment is not the same as correctness: every method on that
    processor now swallows its exceptions so telemetry can never fail a request, and
    a fully-swallowing processor that silently drops everything is its own failure
    mode. `errors > 0` means telemetry is degraded while requests are fine — which
    is exactly the state that would otherwise be invisible.
    """
    proc = _langfuse_processor
    if proc is None:
        return {"attached": False}
    return {
        "attached": True,
        "forwarded": int(getattr(proc, "forwarded", 0)),
        "dropped": int(getattr(proc, "dropped", 0)),
        "errors": int(getattr(proc, "errors", 0)),
    }


def record_op(
    op_name: str,
    *,
    namespace: str,
    success: bool,
    duration_ms: float,
    transport: str = "http",
    error_code: str = "",
) -> None:
    """Emit one metric tick per Loom op. No-op when OTel metrics disabled.

    `transport` is a bounded enum (http|mcp). Without it the HTTP dashboard and
    agent traffic land in the same series and become impossible to tell apart —
    which defeats the point of instrumenting both, since the question worth
    asking is whether agents see different retrieval quality than the UI.
    `error_code` names a failure's code ("" on success) on `loom.requests`, so a
    503 PROJECTION_BEHIND or PUBLICATION_PENDING rate reads as callers saw it.
    """
    try:
        attrs = {
            "loom.operation": op_name,
            "loom.namespace": namespace,
            "loom.transport": transport if transport in ("http", "mcp") else "other",
            "success": "true" if success else "false",
        }
        if _request_counter is not None:
            # The error code on the counter only: a closed set (a LoomServerError code),
            # which would multiply the duration histogram's series for no reader.
            _request_counter.add(1, attributes={**attrs, "loom.error_code": error_code})
        if _request_duration is not None:
            _request_duration.record(duration_ms, attributes=attrs)
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        pass


def traced(name: str, **attrs: Any) -> Any:
    """Context manager that emits a child span of the active request.

    Use inside async handlers to make latency hot-spots visible in the
    HyperDX trace waterfall:

        with traced("ch.insert.memory", table="loom.memory"):
            await ch.insert(...)

    A no-op when OTel is disabled or no parent span is recording —
    returns a context manager that does nothing. Never raises.
    """
    import contextlib

    try:
        from opentelemetry import trace

        # The scope name stays "loom.handlers": dashboards and alerts filter on it.
        tracer = trace.get_tracer("loom.handlers")

        @contextlib.contextmanager
        def _cm() -> Iterator[trace.Span]:
            with tracer.start_as_current_span(name) as span:
                try:
                    for k, v in attrs.items():
                        if v is not None:
                            span.set_attribute(k, v)
                    # Langfuse turns ANY span carrying a `model` attribute into a
                    # GENERATION. Several call sites pass `model=` purely as
                    # context — `traced("embed.compute.query", model=...)` — and
                    # those would arrive as generations with 0 tokens and $0 cost,
                    # inflating the generation count with empty rows and dragging
                    # down every per-generation average. Real generations come
                    # from `genai.py`, which sets this to "generation" itself.
                    if "model" in attrs:
                        span.set_attribute("langfuse.observation.type", "span")
                except Exception:  # noqa: BLE001 — span attrs must not fail the op
                    pass
                yield span

        return _cm()
    except Exception:  # noqa: BLE001 — tracing must never fail the op
        return contextlib.nullcontext()


def worker_pass_span(name: str) -> Any:
    """Root span `worker.<name>.pass` for one background worker pass.

    Workers have no request parent, so `traced()`'s child span would be a
    root anyway — this exists to give the pass a stable name and identity
    (`loom.worker`, `loom.operation=worker.<name>`) so the CH/LLM/embed spans
    it makes nest under something findable, and to skip the `model`
    special-case that only request handlers need. Returns a context manager
    yielding the span (a no-op span when OTel is disabled). Never raises.
    """
    import contextlib

    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("loom.workers")

        @contextlib.contextmanager
        def _cm() -> Iterator[Any]:
            with tracer.start_as_current_span(f"worker.{name}.pass") as span:
                try:
                    span.set_attribute("loom.worker", name)
                    span.set_attribute("loom.operation", f"worker.{name}")
                    span.set_attribute("langfuse.trace.name", f"worker.{name}")
                except Exception:  # noqa: BLE001 — span attrs must not fail the pass
                    pass
                yield span

        return _cm()
    except Exception:  # noqa: BLE001 — tracing must never fail the pass
        return contextlib.nullcontext()


def set_loom_attributes(
    *,
    op_name: str,
    org: str,
    namespace: str,
    agent: str,
    user_id: str | None,
    request_id: str,
    session_id: str | None = None,
    capture_level: str = "",
) -> None:
    """Set loom.* attributes on the currently-active span.

    Called from `_make_endpoint` so every span carries enough identity
    to filter in HyperDX by tenant or op. No-op when no span is
    active (OTel disabled or exporter unconfigured).

    Also publishes the identity to a ContextVar that the spans the request opens
    read (each model-call span, and with Langfuse configured every span).
    OTel children do NOT inherit a parent's attributes, so without that every
    `llm.*` and `embed.*` span would carry an empty `loom.operation`, and "which
    namespace is burning LLM spend" would be unanswerable from traces.
    """
    try:
        from server.observability import genai as _genai

        _genai.set_identity(
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id or "",
            session_id=session_id or "",
            operation=op_name,
            # Resolved by the caller (async, TTL-cached) so the LLM path never
            # queries for it. Empty means unresolved, which falls back to the
            # operator level rather than inventing a consent decision.
            capture_level=capture_level,
        )
    except Exception:  # noqa: BLE001 — identity is additive; never fail a request
        pass
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        span.set_attribute("loom.operation", op_name)
        span.set_attribute("loom.org", org)
        span.set_attribute("loom.namespace", namespace)
        span.set_attribute("loom.agent", agent)
        span.set_attribute("loom.request_id", request_id)
        if user_id:
            span.set_attribute("loom.user_id", user_id)
            # OTel semconv attribute — HyperDX filters by user out of the box.
            span.set_attribute("enduser.id", user_id)
            # Langfuse groups traces by its own key.
            span.set_attribute("langfuse.user.id", user_id)
        if session_id:
            span.set_attribute("loom.session_id", session_id)
            # The real conversation session from `identity`, not the tenant
            # triple: otherwise every conversation would read as one session per
            # agent, and per-conversation drill-down would be impossible.
            span.set_attribute("langfuse.session.id", session_id)
        span.set_attribute("langfuse.trace.tags", [f"org:{org}", f"namespace:{namespace}"])
        # Name the trace explicitly. Left implicit, Langfuse names it after the
        # root span — which is the ASGI span (`POST /v1/memory.search`) and, when
        # children arrive in an earlier batch than the root, nothing at all (a
        # correct trace with an EMPTY name). The loom operation is both stable
        # and more useful than the HTTP route.
        span.set_attribute("langfuse.trace.name", op_name)
    except Exception:  # noqa: BLE001 — trace attrs must never fail a request
        pass


def _quality_histogram_views() -> list[Any]:
    """Explicit bucket boundaries for the retrieval-quality histograms.

    OTel's default boundaries are `[0, 5, 10, 25, … 10000]` — sized for
    millisecond latencies. `loom.search.top_similarity` and
    `loom.search.confidence` are valued 0..1, so with the defaults every
    observation lands in the first bucket and *every* quantile over them
    returns that bucket's midpoint (2.5) no matter what the data says; only
    `Sum/Count` would carry real signal.

    Decile buckets for the 0..1 metrics and a small-count ladder for
    `loom.search.results` make p50/p95 meaningful, so "the median search is
    fine but the 5th percentile is terrible" becomes a question you can ask.

    Returns `[]` if the SDK view API is unavailable, leaving default
    aggregation.
    """
    try:
        from opentelemetry.sdk.metrics import Histogram
        from opentelemetry.sdk.metrics.view import (
            ExplicitBucketHistogramAggregation,
            View,
        )
    except ImportError as e:  # pragma: no cover — SDK shape guard
        log.warning("histogram views unavailable, using default buckets: %s", e)
        return []

    unit_scale = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    # Result counts are small and the interesting boundary is "did it return
    # anything at all", so the ladder is dense at the bottom.
    count_scale = [0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 34.0, 55.0, 100.0]
    return [
        View(
            instrument_type=Histogram,
            instrument_name=name,
            aggregation=ExplicitBucketHistogramAggregation(boundaries=bounds),
        )
        for name, bounds in (
            ("loom.search.top_similarity", unit_scale),
            ("loom.search.confidence", unit_scale),
            ("loom.search.judged_relevance", unit_scale),
            ("loom.search.results", count_scale),
        )
    ]


def set_search_attributes(
    *,
    hits: int,
    confidence: float | None,
    top_similarity: float | None,
    abstain: bool,
    abstain_reason: str,
    assists: list[str],
) -> None:
    """Mirror a search's retrieval outcome onto the currently-active span.

    The same numbers already land in `loom.search.*` metrics, but a histogram
    can only be read in aggregate: a falling top-similarity line tells you
    retrieval got worse and gives you no way to reach the requests that made it
    worse. These attributes are what make the quality panels clickable, and
    they are the only way to answer "show me the searches that abstained" —
    an abstention is invisible in latency and error rate, which both look
    healthy while Loom returns nothing useful.

    Bounded values only, same discipline as the metric attributes: counts,
    floats, a capped reason string and a closed set of assist names. No query
    text — spans are sampled but not private, and the query is user content.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span is None or not span.is_recording():
            return
        span.set_attribute("loom.search.results", int(hits))
        span.set_attribute("loom.search.abstain", bool(abstain))
        if abstain and abstain_reason:
            span.set_attribute("loom.search.abstain_reason", abstain_reason[:48])
        if confidence is not None:
            span.set_attribute("loom.search.confidence", float(confidence))
        if top_similarity is not None:
            span.set_attribute("loom.search.top_similarity", float(top_similarity))
        if assists:
            # Comma-joined rather than an array: HyperDX filters string
            # attributes with `contains`, and Map(String,String) columns can't
            # hold an array anyway.
            span.set_attribute("loom.search.assists", ",".join(sorted(assists)))
    except Exception:  # noqa: BLE001 — span attrs must never fail a search
        pass


__all__ = [
    "init_otel",
    "record_op",
    "langfuse_processor_status",
    "set_loom_attributes",
    "set_search_attributes",
    "shutdown",
    "traced",
    "worker_pass_span",
]
