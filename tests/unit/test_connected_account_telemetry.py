"""Consent URLs must be excluded before ASGI instrumentation opens a server span."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from server.knowledge.app import connection_routes as routes
from server.observability import otel
from server.settings import settings


@pytest.mark.asyncio
@pytest.mark.parametrize("method,action", [("POST", "start"), ("GET", "callback")])
async def test_consent_is_excluded_before_server_span(
    monkeypatch: pytest.MonkeyPatch, method: str, action: str
) -> None:
    monkeypatch.setattr(settings, "otel_excluded_urls", "")
    monkeypatch.setattr(settings, "connect_notion_client_id", "")
    monkeypatch.setattr(settings, "connect_notion_client_secret", "")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE", ".*")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrument = FastAPIInstrumentor.instrument_app

    def local_instrument(app: FastAPI, **kwargs: Any) -> None:
        # Keep the real instrumentor and source exclusions, with a local collection
        # sink instead of changing the process's global tracing provider.
        instrument(app, tracer_provider=provider, **kwargs)

    monkeypatch.setattr(FastAPIInstrumentor, "instrument_app", local_instrument)
    app = FastAPI()
    routes.attach(app)

    @app.get("/control")
    async def control() -> dict[str, bool]:
        return {"ok": True}

    otel._instrument_fastapi(app)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://inert.invalid",
        ) as client:
            control_response = await client.get("/control")
            assert control_response.status_code == 200
            provider.force_flush()
            assert exporter.get_finished_spans(), "the nonexcluded control must emit a span"
            exporter.clear()
            response = await client.request(
                method,
                f"/v1/connect/notion/{action}?code=private-code-proof&state=private-state-proof",
                headers={"X-OAuth-Proof": "private-header-proof"},
            )
        # Real route validation rejects inert credentials/config before provider or
        # storage work. Its errors must also create no pre-handler server/child spans.
        assert response.status_code >= 400
        provider.force_flush()
        assert not exporter.get_finished_spans()
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
        provider.shutdown()
