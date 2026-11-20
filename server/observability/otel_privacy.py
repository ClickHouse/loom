"""Gate request diagnostics before processors queue spans or histograms aggregate."""

from __future__ import annotations

from typing import Any

from server.core.diagnostics import public_diagnostics


def public_span_processor(inner: Any) -> Any:
    """Retain the SDK lifecycle while keeping private spans out of its queue."""
    from opentelemetry.sdk.trace import SpanProcessor

    class PublicSpanProcessor(SpanProcessor):
        def on_start(self, span: Any, parent_context: Any = None) -> None:
            if public_diagnostics():
                inner.on_start(span, parent_context)

        def on_end(self, span: Any) -> None:
            if public_diagnostics():
                inner.on_end(span)

        def shutdown(self) -> None:
            inner.shutdown()

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return bool(inner.force_flush(timeout_millis))

    return PublicSpanProcessor()


def asgi_meter_provider(inner: Any) -> Any:
    """Guard ASGI histograms without changing its participation counters."""
    from opentelemetry.metrics import Histogram, Meter, MeterProvider

    class PublicHistogram(Histogram):
        def __init__(self, instrument: Any) -> None:
            self._instrument = instrument

        def record(self, amount: float, attributes: Any = None, context: Any = None) -> None:
            if public_diagnostics():
                self._instrument.record(amount, attributes, context)

    class ASGIMeter(Meter):
        def __init__(
            self, meter: Any, name: str, version: str | None, schema_url: str | None
        ) -> None:
            super().__init__(name, version, schema_url)
            self._meter = meter

        def create_histogram(
            self,
            name: str,
            unit: str = "",
            description: str = "",
            explicit_bucket_boundaries_advisory: Any = None,
        ) -> Any:
            return PublicHistogram(
                self._meter.create_histogram(
                    name,
                    unit=unit,
                    description=description,
                    explicit_bucket_boundaries_advisory=explicit_bucket_boundaries_advisory,
                )
            )

        def create_counter(self, *args: Any, **kwargs: Any) -> Any:
            return self._meter.create_counter(*args, **kwargs)

        def create_up_down_counter(self, *args: Any, **kwargs: Any) -> Any:
            return self._meter.create_up_down_counter(*args, **kwargs)

        def create_observable_counter(self, *args: Any, **kwargs: Any) -> Any:
            return self._meter.create_observable_counter(*args, **kwargs)

        def create_observable_up_down_counter(self, *args: Any, **kwargs: Any) -> Any:
            return self._meter.create_observable_up_down_counter(*args, **kwargs)

        def create_gauge(self, *args: Any, **kwargs: Any) -> Any:
            return self._meter.create_gauge(*args, **kwargs)

        def create_observable_gauge(self, *args: Any, **kwargs: Any) -> Any:
            return self._meter.create_observable_gauge(*args, **kwargs)

    class ASGIMeterProvider(MeterProvider):
        def get_meter(
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
            attributes: Any = None,
        ) -> Any:
            meter = inner.get_meter(name, version, schema_url, attributes)
            return ASGIMeter(meter, name, version, schema_url)

    return ASGIMeterProvider()
