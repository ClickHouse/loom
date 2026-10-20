"""A LoomServerError carrying `retry_after_s` (503 OVERLOADED) answers with a
`Retry-After` header as well as the body field, so every client and proxy
reads the hint the SDKs honour."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

import server.transport.http as http
from server.core.errors import NotFoundError, OverloadedError
from server.operations.base import Operation, Role


class _In(BaseModel):
    org: str = "acme"


class _Out(BaseModel):
    ok: bool = True


def _raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> HTTPException:
    monkeypatch.setattr(http, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(http, "_record_failure", lambda *a, **k: None)

    async def _handler(inp: Any) -> _Out:
        raise exc

    op = Operation(
        name="test.refused",
        input_model=_In,
        output_model=_Out,
        handler=_handler,
        idempotent=True,
        required_role=Role.VIEWER,
    )
    call = http._HttpCall(request_id="rid-1", identity={"namespace": "ns"})
    with pytest.raises(HTTPException) as info:
        asyncio.run(http._run_handler(op, _In(), call))
    return info.value


def test_overloaded_sends_retry_after_with_the_body_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    he = _raise(monkeypatch, OverloadedError("pool busy", retry_after_s=3))
    assert he.status_code == 503
    assert isinstance(he.detail, dict) and he.detail["retry_after_s"] == 3
    assert he.headers == {"Retry-After": "3"}


def test_an_error_without_a_hint_sends_no_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    he = _raise(monkeypatch, NotFoundError("gone"))
    assert he.status_code == 404 and not he.headers


def test_the_app_handler_sends_retry_after_outside_an_op() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.core.errors import register_exception_handler

    app = FastAPI()
    register_exception_handler(app)

    @app.get("/busy")
    async def _busy() -> None:
        raise OverloadedError("pool busy", retry_after_s=2)

    @app.get("/gone")
    async def _gone() -> None:
        raise NotFoundError("gone")

    client = TestClient(app)
    busy = client.get("/busy")
    assert busy.status_code == 503 and busy.headers["Retry-After"] == "2"
    assert busy.json()["detail"]["retry_after_s"] == 2
    assert "Retry-After" not in client.get("/gone").headers
