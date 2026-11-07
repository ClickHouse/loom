"""The Langfuse span processor: a filtered, trace-sampled subset of spans to
Langfuse's OTLP endpoint, beside the ClickHouse processor `otel.init_otel` adds."""

from __future__ import annotations

import logging
from typing import Any

from server.observability import genai
from server.settings import settings

log = logging.getLogger(__name__)


def _langfuse_otlp_endpoint() -> str | None:
    """Langfuse's OTLP trace endpoint, or None if Langfuse is unconfigured."""
    base = settings.langfuse_url.strip().rstrip("/")
    if not base:
        return None
    return f"{base}/api/public/otel/v1/traces"


def _langfuse_otlp_headers() -> dict[str, str] | None:
    """Basic auth on the same `pk:sk` the scores client uses.

    Langfuse's OTLP endpoint takes HTTP Basic with the public key as username and
    the secret key as password — not a bearer token, which is the mistake this
    helper exists to prevent being made twice.
    """
    token = settings.langfuse_admin_token.strip()
    if ":" not in token:
        return None
    import base64

    pk, sk = token.split(":", 1)
    encoded = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        # Pins the ingest schema. Without it Langfuse applies its legacy
        # mapping, in which the GenAI attributes are not read as usage.
        "x-langfuse-ingestion-version": "4",
    }


class _LangfuseSpanFilter:
    """Decides which spans Langfuse gets. Pure logic, no OTel import.

    Separated from the processor so the rules can be unit-tested directly, and so
    the processor itself stays a thin SDK subclass — a processor that duck-types
    the interface misses the SDK's private `_on_ending` hook and breaks every
    span end in the process.

    Filtering lives here rather than in an OTel `Sampler` because a Sampler sits
    on the provider and would drop the span for *both* exporters. Langfuse should
    see less than ClickStack does, not the same thing.

    Sampling is keyed on the trace id, so a sampled trace arrives whole. A
    per-span coin flip would ship orphaned children and make every waterfall a
    lie — and would make per-trace cost sums wrong, which is the one number this
    integration exists to provide.
    """

    def __init__(self) -> None:
        self._denylist = tuple(
            p.strip() for p in settings.langfuse_span_denylist.split(",") if p.strip()
        )
        rate = settings.langfuse_sample_rate
        self.rate = 0.0 if rate < 0 else (1.0 if rate > 1 else float(rate))
        # W3C-style consistent sampling: compare the low 64 bits of the trace id
        # against a threshold. Deterministic, so every service and every span in
        # the trace reaches the same verdict without coordination.
        self._threshold = int(self.rate * (1 << 64))

    # Bare HTTP-verb span names are httpx CLIENT spans — the outbound call to
    # OpenAI or Anthropic. They duplicate the generation span that already wraps
    # the same request, and at trace root (workers) each one becomes its own
    # nameless single-span trace.
    _CLIENT_SPAN_NAMES = frozenset({"POST", "GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
    # ASGI lifecycle sub-spans. Pure transport mechanics, three per request.
    _ASGI_SUFFIXES = (" http send", " http receive")

    def keep(self, span: Any) -> bool:
        name = span.name or ""
        if name in self._CLIENT_SPAN_NAMES:
            return False
        if name.endswith(self._ASGI_SUFFIXES):
            return False
        if self._denylist and name.startswith(self._denylist):
            return False
        # Worker-backfill embeddings: a request-path embed nests inside a handler
        # span so it has a parent, while the embedding worker calls the embedder
        # directly at trace root. Dropping roots keeps "the embedding this search
        # paid for" and discards "the backfill queue draining at 12/s", which is
        # ~97% of all span volume and belongs in a metric, not a trace UI.
        if name.startswith("embed.") and span.parent is None:
            return False
        # Worker pass roots (`worker.<name>.pass`, from `record_pass`): 25
        # workers, some on 5-30s intervals, each pass a one-span trace with no
        # generation in it. They belong in the ClickStack waterfall and the
        # `loom.worker.*` metrics, not as Langfuse traces. The generations a
        # pass DOES make still arrive, and carry `langfuse.trace.name =
        # worker.<name>` plus loom.org/namespace from the worker identity.
        if name.startswith("worker.") and name.endswith(".pass") and span.parent is None:
            return False
        if self.rate >= 1.0:
            return True
        if self.rate <= 0.0:
            return False
        ctx = span.get_span_context()
        return bool((ctx.trace_id & 0xFFFFFFFFFFFFFFFF) < self._threshold)


def _langfuse_processor_class() -> Any:
    """Build the processor class against the installed SDK.

    A real `SpanProcessor` subclass, not a duck-typed object: the SDK calls
    private hooks on processors (`_on_ending`, added in a recent version) and
    reaches them through the multi-processor for EVERY span end. A stand-in that
    merely implements the public four would raise AttributeError on every span.
    Subclassing inherits current and future hooks.

    Built lazily because this module must import without the OTel SDK present.
    """
    from opentelemetry.sdk.trace import SpanProcessor

    class _LangfuseSpanProcessor(SpanProcessor):
        """Forwards a filtered, trace-sampled subset of spans to Langfuse.

        A second `BatchSpanProcessor` on the same `TracerProvider` rather than a
        second exporter mode: `init_otel` in `otlp` mode sets
        `log_exporter = None` and skips the metrics block, so switching modes
        would silently drop every log line and all 14 domain metrics. Two
        processors keep ClickHouse whole and make Langfuse purely additive — if
        Langfuse is down, its own queue drops and the ClickHouse path never
        notices.
        """

        def __init__(self, inner: Any, span_filter: Any) -> None:
            self._inner = inner
            self._filter = span_filter
            self.dropped = 0
            self.forwarded = 0
            # Suppressed exception count. Contained is not the same as invisible:
            # a processor that silently swallows everything is its own failure
            # mode, so surface it via `_probe_state`/logs instead.
            self.errors = 0

        def _suppress(self, where: str, exc: BaseException) -> None:
            self.errors += 1
            # First few, then decimate — a broken processor fires per span and
            # would otherwise drown the log it is trying to report through.
            if self.errors <= 3 or self.errors % 1000 == 0:
                log.warning(
                    "Langfuse span processor error in %s (%s: %s); suppressed, "
                    "%d total. Telemetry is degraded but requests are unaffected.",
                    where,
                    type(exc).__name__,
                    exc,
                    self.errors,
                )

        # EVERY method below is total. The OTel SDK calls `_on_ending` and
        # `on_end` from `Span.end()` WITHOUT a guard of its own, so an exception
        # raised here propagates into whatever ended the span — a request handler.
        # Total containment: no bug in here, or in the inner BatchSpanProcessor,
        # can fail a request. Losing telemetry is always the better failure.

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            try:
                # Set at start, while the span is still writable; the ClickHouse
                # export sees these keys too, but only when Langfuse is configured.
                genai.stamp_identity(span)
            except Exception as e:  # noqa: BLE001 — identity is additive; the span still exports
                self._suppress("identity", e)
            try:
                self._inner.on_start(span, parent_context)
            except Exception as e:  # noqa: BLE001 — the Langfuse processor never fails a span
                self._suppress("on_start", e)

        def _on_ending(self, span: Any) -> None:
            # Private SDK hook, called on every span end. Overridden purely to
            # guarantee it cannot raise, whatever a future SDK version does with it.
            try:
                inner = getattr(self._inner, "_on_ending", None)
                if callable(inner):
                    inner(span)
            except Exception as e:  # noqa: BLE001 — the Langfuse processor never fails a span
                self._suppress("_on_ending", e)

        def on_end(self, span: Any) -> None:
            try:
                if not self._filter.keep(span):
                    self.dropped += 1
                    return
                self.forwarded += 1
            except Exception as e:  # noqa: BLE001 — a filter bug must not stop export
                self._suppress("filter", e)
            try:
                self._inner.on_end(span)
            except Exception as e:  # noqa: BLE001 — the Langfuse processor never fails a span
                # A full queue or a shutdown race inside BatchSpanProcessor
                # would otherwise propagate into the request.
                self._suppress("on_end", e)

        def shutdown(self) -> None:
            try:
                self._inner.shutdown()
            except Exception as e:  # noqa: BLE001 — a failed flush must not block exit
                self._suppress("shutdown", e)

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            try:
                return bool(self._inner.force_flush(timeout_millis))
            except Exception as e:  # noqa: BLE001 — a failed flush must not block exit
                self._suppress("force_flush", e)
                return False

    return _LangfuseSpanProcessor


def _add_langfuse_processor(provider: Any) -> Any:
    """Attach the Langfuse OTLP processor whenever Langfuse is configured.
    Returns it, or None when it is not."""
    endpoint = _langfuse_otlp_endpoint()
    headers = _langfuse_otlp_headers()
    if not endpoint or not headers:
        if settings.langfuse_url:
            log.warning(
                "langfuse_url is set but langfuse_admin_token is not 'pk-lf-...:sk-lf-...'; "
                "Langfuse span export disabled"
            )
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        log.warning("OTLP HTTP exporter unavailable (%s); Langfuse export off", e)
        return None

    try:
        proc = _langfuse_processor_class()(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, headers=headers),
                schedule_delay_millis=5000,
                # A third party being slow must not back-pressure loom. The
                # ClickHouse processor has its own independent queue.
                max_queue_size=4096,
            ),
            _LangfuseSpanFilter(),
        )
    except Exception as e:  # noqa: BLE001 — Langfuse export is optional; boot continues without it
        log.warning("Langfuse span processor init failed (%s); disabled", e)
        return None

    provider.add_span_processor(proc)
    log.info(
        "Langfuse OTLP span export enabled — %s (sample_rate=%.3f, deny=%s)",
        endpoint,
        proc._filter.rate,
        settings.langfuse_span_denylist or "-",
    )
    return proc
