"""Regression: a handler exception that falls into the generic
INTERNAL_ERROR path MUST log the full traceback server-side.

The HTTP response carries only the request id (never ``str(e)``, which
can hold ClickHouse / SQL text), so without a ``log.exception`` in the
generic handler the failure is undiagnosable from the server logs.

The response shape is also locked here.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from server.operations.base import Operation, Role
from server.transport import http as http_mod


class _ProbeInput(BaseModel):
    org: str = "-"
    namespace: str = "-"


class _ProbeOutput(BaseModel):
    ok: bool = True


async def _raises_keyerror(_: _ProbeInput) -> _ProbeOutput:
    # A raw dict subscript on a row missing the column.
    raise KeyError("memory_id")


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    op = Operation(
        name="probe.internal_error",
        input_model=_ProbeInput,
        output_model=_ProbeOutput,
        handler=_raises_keyerror,
        idempotent=False,
        required_role=Role.VIEWER,
    )

    # No CH in unit tests — agent-event emission is out of scope here.
    def _no_emit(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(http_mod, "_emit_event", _no_emit)
    # Force the anonymous path regardless of the local .env.
    monkeypatch.setattr(http_mod.settings, "auth_required", False)
    monkeypatch.setattr(
        type(http_mod.settings),
        "auth_actually_required",
        lambda self: False,
        raising=False,
    )

    app = FastAPI()
    app.add_api_route(op.http_path, http_mod._make_endpoint(op), methods=["POST"])
    return TestClient(app, raise_server_exceptions=False)


def test_internal_error_logs_traceback_and_keeps_response_shape(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="server.transport.http"):
        resp = client.post("/v1/probe.internal_error", json={})

    # 500 + code + a message naming the request id, not the exception.
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "INTERNAL_ERROR"
    assert detail["request_id"]
    assert detail["message"] == f"internal error; request_id={detail['request_id']}"

    # An ERROR record with the traceback.
    records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "op.internal_error" in r.getMessage()
    ]
    assert records, "generic INTERNAL_ERROR path logged nothing"
    record = records[0]
    assert record.exc_info is not None
    assert "KeyError" in caplog.text
    # The traceback names the raising frame, not just the message.
    assert "_raises_keyerror" in caplog.text


# ---------------------------------------------------------------------------
# `loom.requests{success="false"}` for HTTP.
# Every failure branch must tick the counter with the SAME attribute set as
# the success tick, or the HTTP error series is empty and every error-rate
# panel divides by zero.
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


def _client_for(monkeypatch: pytest.MonkeyPatch, handler: Any, name: str) -> TestClient:
    op = Operation(
        name=name,
        input_model=_ProbeInput,
        output_model=_ProbeOutput,
        handler=handler,
        idempotent=False,
        required_role=Role.VIEWER,
    )
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(http_mod.settings, "auth_required", False)
    monkeypatch.setattr(
        type(http_mod.settings), "auth_actually_required", lambda self: False, raising=False
    )
    app = FastAPI()
    app.add_api_route(op.http_path, http_mod._make_endpoint(op), methods=["POST"])
    return TestClient(app, raise_server_exceptions=False)


async def _raises_not_found(_: _ProbeInput) -> _ProbeOutput:
    from server.core.errors import NotFoundError

    raise NotFoundError("no such thing")


async def _raises_http(_: _ProbeInput) -> _ProbeOutput:
    from fastapi import HTTPException

    raise HTTPException(status_code=409, detail="conflict")


@pytest.mark.parametrize(
    ("handler", "status"),
    [
        (_raises_keyerror, 500),  # generic INTERNAL_ERROR branch
        (_raises_not_found, 404),  # LoomServerError branch
        (_raises_http, 409),  # HTTPException branch
    ],
)
def test_every_http_failure_path_records_a_failed_op(
    monkeypatch: pytest.MonkeyPatch, handler: Any, status: int
) -> None:
    from server.observability import otel as otel_mod

    rec = _Recorder()
    monkeypatch.setattr(otel_mod, "record_op", rec)
    client = _client_for(monkeypatch, handler, "probe.failure_metric")

    resp = client.post("/v1/probe.failure_metric", json={"namespace": "ns-x"})
    assert resp.status_code == status

    failures = [c for c in rec.calls if c[1].get("success") is False]
    assert len(failures) == 1, f"expected exactly one failed record_op, got {rec.calls}"
    args, kwargs = failures[0]
    assert args == ("probe.failure_metric",)
    # Same attribute set as the success tick: op name, namespace, duration,
    # default transport — so success and failure series divide cleanly.
    assert kwargs["namespace"] == "ns-x"
    assert isinstance(kwargs["duration_ms"], int)
    # `error_code` goes on the request counter alone, never on the duration histogram
    # (`otel.record_op`), so the two series still divide.
    assert set(kwargs) == {"namespace", "success", "duration_ms", "error_code"}
    assert kwargs["error_code"]
    # No stray success tick on a failed request.
    assert not any(c[1].get("success") is True for c in rec.calls)


def test_successful_op_still_records_exactly_one_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import otel as otel_mod

    async def _ok(_: _ProbeInput) -> _ProbeOutput:
        return _ProbeOutput()

    rec = _Recorder()
    monkeypatch.setattr(otel_mod, "record_op", rec)
    client = _client_for(monkeypatch, _ok, "probe.success_metric")
    assert client.post("/v1/probe.success_metric", json={}).status_code == 200
    assert [c[1]["success"] for c in rec.calls] == [True]
