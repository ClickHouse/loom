"""Unit coverage for the OTel traces-backend gate.

`_traces_backend_available` decides whether to auto-instrument FastAPI. It must
return False when there's nowhere to send spans — otherwise instrumenting is
pure overhead and, on CI's opentelemetry-instrumentation-asgi version, 500s the
CORS OPTIONS preflight ('_IncludedRouter' has no attribute 'path'). Pure
function + a stubbed CH client; no real CH or OTel packages needed.
"""

from __future__ import annotations

import pytest

from server.observability import otel_exporters


def test_otlp_mode_needs_an_endpoint() -> None:
    assert otel_exporters._traces_backend_available("otlp", "http://collector:4317") is True
    assert otel_exporters._traces_backend_available("otlp", "") is False
    assert otel_exporters._traces_backend_available("otlp", None) is False


class _StubClient:
    def __init__(self, exists_result: object) -> None:
        self._r = exists_result
        self.closed = False

    def command(self, _sql: str) -> object:
        return self._r

    def close(self) -> None:
        self.closed = True


def test_ch_mode_true_only_when_traces_table_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    # CH returns "1" → table present → instrument.
    c = _StubClient("1")
    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: c)
    assert otel_exporters._traces_backend_available("ch", None) is True
    assert c.closed is True  # client is always closed

    # "0" → absent (the CI / CH-only-deploy case) → skip.
    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", lambda: _StubClient("0"))
    assert otel_exporters._traces_backend_available("ch", None) is False


def test_ch_mode_fails_safe_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any probe error (CH unreachable, perms, etc.) must disable, not raise.
    def _boom() -> None:
        raise RuntimeError("CH down")

    monkeypatch.setattr(otel_exporters, "_make_ch_client_for_otel", _boom)
    assert otel_exporters._traces_backend_available("ch", None) is False
