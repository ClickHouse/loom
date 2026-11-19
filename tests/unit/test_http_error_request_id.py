"""The actual HTTP response keeps the id bound to logs and native query ids."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from server.core.ch import current_query_tag
from server.core.ch_request_cost import request_query_id
from server.core.errors import VectorAdmissionError, error_help_link, register_exception_handler
from server.core.logging_config import request_id_var
from server.main import _response_headers
from server.observability import capture_consent
from server.operations.base import Operation, Role
from server.transport import http


class _Input(BaseModel):
    org: str = "-"
    namespace: str = "-"


class _Output(BaseModel):
    ok: bool = True


@pytest.mark.parametrize("caller_id", ["caller-123._", "invalid id", None])
@pytest.mark.parametrize(
    ("failure", "status", "code", "retry_after"),
    [
        ("vector", 429, "VECTOR_CAPACITY_REFUSED", "1"),
        ("http", 409, "CONFLICT", None),
        ("internal", 500, "INTERNAL_ERROR", None),
        ("success", 200, None, None),
    ],
)
def test_response_keeps_the_bound_request_id(
    monkeypatch: pytest.MonkeyPatch,
    caller_id: str | None,
    failure: str,
    status: int,
    code: str | None,
    retry_after: str | None,
) -> None:
    bound: dict[str, str] = {}

    async def handler(_: _Input) -> _Output:
        bound["id"] = request_id_var.get()
        bound["tag"] = current_query_tag()
        query_id = request_query_id(bound["tag"])
        assert query_id is not None
        bound["query_id"] = query_id
        if failure == "vector":
            raise VectorAdmissionError("row budget exhausted", reason="rows")
        if failure == "http":
            raise HTTPException(409, detail={"code": "CONFLICT", "message": "conflict"})
        if failure == "internal":
            raise RuntimeError("private failure")
        return _Output()

    async def no_post_handler_rows(*args: Any, **kwargs: Any) -> None:
        pass

    async def capture_off(org: str, namespace: str) -> str:
        return "off"

    op = Operation(
        name="probe.request_id",
        input_model=_Input,
        output_model=_Output,
        handler=handler,
        idempotent=False,
        required_role=Role.VIEWER,
    )
    monkeypatch.setattr(http, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(http, "_record_failure", lambda *a, **k: None)
    monkeypatch.setattr(http, "post_handler_rows", no_post_handler_rows)
    monkeypatch.setattr(capture_consent, "resolve", capture_off)
    monkeypatch.setattr(http.settings, "auth_required", False)
    monkeypatch.setattr(type(http.settings), "auth_actually_required", lambda self: False)
    app = FastAPI()
    app.middleware("http")(_response_headers)
    register_exception_handler(app)
    app.add_api_route(op.http_path, http._make_endpoint(op), methods=["POST"])
    headers = {"X-Request-Id": caller_id} if caller_id is not None else {}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(op.http_path, json={}, headers=headers)

    assert response.status_code == status
    response_id = response.headers["X-Request-Id"]
    assert response_id == bound["id"]
    if caller_id == "caller-123._":
        assert response_id == caller_id
    else:
        assert str(UUID(response_id)) == response_id
        assert response_id != caller_id
    assert bound["tag"] == f"{op.name} {response_id}"
    assert bound["query_id"].startswith(f"{response_id}:1:")
    assert response.headers.get("Retry-After") == retry_after
    if failure == "success":
        assert response.json()["ok"] is True
        return
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert response.headers["Link"] == error_help_link(str(code))
    if failure == "vector":
        assert detail["reason"] == "rows"
        assert detail["retry_after_s"] == 1
        assert detail["retryable"] is True
    if failure == "internal":
        assert detail["request_id"] == response_id
        assert detail["message"] == f"internal error; request_id={response_id}"
        assert detail["retryable"] is False
        assert "private failure" not in response.text


@pytest.mark.parametrize("caller_id", [None, "invalid id"])
@pytest.mark.parametrize("status", [200, 409])
def test_me_response_keeps_its_own_bound_id(
    monkeypatch: pytest.MonkeyPatch, caller_id: str | None, status: int
) -> None:
    bound: dict[str, str] = {}
    ledger: list[dict[str, Any]] = []
    monkeypatch.setattr(http, "emit_agent_event", lambda **kw: ledger.append(kw))
    app = FastAPI()
    register_exception_handler(app)

    @app.get("/v1/me/probe")
    async def probe() -> dict[str, bool]:
        bound["id"] = request_id_var.get()
        bound["tag"] = current_query_tag()
        query_id = request_query_id(bound["tag"])
        assert query_id is not None
        bound["query_id"] = query_id
        if status == 409:
            raise HTTPException(409, detail={"code": "CONFLICT", "message": "conflict"})
        return {"ok": True}

    # The production order: measured /me wraps the common response middleware.
    app.middleware("http")(_response_headers)
    app.middleware("http")(http.me_route_measured)
    headers = {"X-Request-Id": caller_id} if caller_id is not None else {}
    with TestClient(app) as client:
        response = client.get("/v1/me/probe", headers=headers)
    assert response.status_code == status
    request_id = response.headers["X-Request-Id"]
    assert str(UUID(request_id)) == request_id
    assert request_id == bound["id"] == ledger[0]["request_id"]
    assert bound["tag"] == f"me.probe {request_id}"
    assert bound["query_id"].startswith(f"{request_id}:1:")
    assert ledger[0]["success"] is (status == 200)
    if status == 409:
        assert response.json()["detail"]["code"] == "CONFLICT"
        assert response.headers["Link"] == error_help_link("CONFLICT")
    else:
        assert response.json() == {"ok": True}


def test_existing_response_request_id_is_preserved() -> None:
    app = FastAPI()
    app.middleware("http")(_response_headers)

    @app.get("/probe")
    async def probe() -> JSONResponse:
        return JSONResponse({"ok": True}, headers={"X-Request-Id": "route-bound"})

    with TestClient(app) as client:
        response = client.get("/probe")
    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "route-bound"
