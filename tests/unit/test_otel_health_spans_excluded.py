"""The FastAPI instrumentation opens no span for
`GET /v1/health`. The healthcheck poll would be a steady stream of span rows
(a root plus its `http send` children per poll) with no reader. Excluding the URL at the instrumentor drops the root and, with it,
the ASGI children — a Sampler would have had to see each of them."""

from __future__ import annotations

import sys
import types

import pytest

from server.observability import otel


def test_default_excludes_the_health_path(settings_override) -> None:
    assert otel._fastapi_excluded_urls() == "/v1/health"
    settings_override(otel_excluded_urls="")
    assert otel._fastapi_excluded_urls() is None
    settings_override(otel_excluded_urls=" /v1/health,/me/assets ")
    assert otel._fastapi_excluded_urls() == "/v1/health,/me/assets"


def _fake_instrumentor(calls: list[dict]) -> types.ModuleType:
    mod = types.ModuleType("opentelemetry.instrumentation.fastapi")

    class FastAPIInstrumentor:
        @staticmethod
        def instrument_app(app, **kwargs):
            calls.append({"app": app, **kwargs})

    mod.FastAPIInstrumentor = FastAPIInstrumentor  # type: ignore[attr-defined]
    return mod


def test_instrument_app_receives_the_excluded_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    monkeypatch.setitem(
        sys.modules, "opentelemetry.instrumentation.fastapi", _fake_instrumentor(calls)
    )
    app = object()
    otel._instrument_fastapi(app)
    assert calls == [{"app": app, "excluded_urls": "/v1/health"}]


def test_a_blank_setting_instruments_every_path(
    monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    calls: list[dict] = []
    monkeypatch.setitem(
        sys.modules, "opentelemetry.instrumentation.fastapi", _fake_instrumentor(calls)
    )
    settings_override(otel_excluded_urls="")
    otel._instrument_fastapi(object())
    assert calls[0]["excluded_urls"] is None


def test_an_instrumentor_failure_never_fails_the_boot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mod = types.ModuleType("opentelemetry.instrumentation.fastapi")

    class FastAPIInstrumentor:
        @staticmethod
        def instrument_app(app, **kwargs):
            raise RuntimeError("boom")

    mod.FastAPIInstrumentor = FastAPIInstrumentor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.fastapi", mod)
    with caplog.at_level("WARNING"):
        otel._instrument_fastapi(object())  # no raise
    assert any("instrument_app failed" in r.getMessage() for r in caplog.records)


def test_the_health_url_matches_the_instrumentors_exclusion_rule() -> None:
    """The instrumentor compiles `excluded_urls` into a regex list and tests
    the full URL with `search`; `/v1/health?include=workers` must match and
    an op path must not."""
    from opentelemetry.util.http import ExcludeList, parse_excluded_urls

    ex: ExcludeList = parse_excluded_urls(otel._fastapi_excluded_urls() or "")
    assert ex.url_disabled("http://127.0.0.1:7777/v1/health")
    assert ex.url_disabled("http://127.0.0.1:7777/v1/health?include=workers")
    assert not ex.url_disabled("http://127.0.0.1:7777/v1/memory.get")
    assert not ex.url_disabled("http://127.0.0.1:7777/v1/memory.search")
