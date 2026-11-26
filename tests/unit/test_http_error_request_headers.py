"""Operation failures keep their correlation metadata through response replacement."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel

import server.transport.http as http
from server.core.errors import OverloadedError, ValidationError, register_exception_handler
from server.operations.base import Operation, Role


class _Input(BaseModel):
    org: str = "fixture"


def _operation() -> Operation:
    async def never(_: Any) -> _Input:
        raise AssertionError("Admission refusal must not run a handler")

    return Operation(
        name="test.error_headers",
        input_model=_Input,
        output_model=_Input,
        handler=never,
        idempotent=True,
        required_role=Role.VIEWER,
    )


@pytest.mark.parametrize("caller_id", ["recorded-request-1", "invalid caller id"])
@pytest.mark.parametrize(
    "error",
    [
        HTTPException(422, {"code": "VALIDATION_FAILED", "message": "invalid cursor"}),
        ValidationError("invalid cursor"),
        HTTPException(401, {"code": "AUTH_INVALID"}, headers={"WWW-Authenticate": "Bearer"}),
        OverloadedError("busy", retry_after_s=3),
    ],
)
def test_error_response_keeps_bound_metadata_without_success_framing(
    caller_id: str, error: HTTPException | ValidationError | OverloadedError
) -> None:
    op = _operation()

    async def refuse(request: Request, response: Response) -> None:
        http._begin_request(op, request, response)
        response.headers["X-Trace-Id"] = "a" * 32
        response.headers["X-RateLimit-Limit"] = "10"
        response.headers["X-RateLimit-Remaining"] = "0"
        response.headers["X-RateLimit-Reset"] = "3"
        response.set_cookie("success-only", "never-copy")
        raise error

    app = FastAPI()
    register_exception_handler(app)
    app.add_api_route("/probe", http._with_error_headers(refuse), methods=["POST"])
    with TestClient(app) as client:
        result = client.post("/probe", headers={"X-Request-Id": caller_id})
    assert result.status_code == error.status_code
    assert result.headers["X-Schema-Version"] == http.SCHEMA_VERSION
    assert result.headers["X-Trace-Id"] == "a" * 32
    assert result.headers["X-RateLimit-Remaining"] == "0"
    assert "set-cookie" not in result.headers
    assert int(result.headers["content-length"]) == len(result.content)
    returned_id = result.headers["X-Request-Id"]
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", returned_id)
    if caller_id == "recorded-request-1":
        assert returned_id == caller_id
    else:
        assert returned_id != caller_id
    if isinstance(error, OverloadedError):
        assert result.headers["Retry-After"] == "3"
    if isinstance(error, HTTPException) and error.status_code == 401:
        assert result.headers["WWW-Authenticate"] == "Bearer"


def test_registered_operation_auth_refusal_keeps_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(_: Request) -> None:
        raise HTTPException(401, {"code": "AUTH_INVALID"})

    monkeypatch.setattr(http, "_extract_auth", refuse)
    app = FastAPI()
    register_exception_handler(app)
    app.add_api_route("/probe", http._make_endpoint(_operation()), methods=["POST"])
    with TestClient(app) as client:
        result = client.post("/probe", headers={"X-Request-Id": "auth-refusal"})
    assert result.status_code == 401
    assert result.headers["X-Request-Id"] == "auth-refusal"
    assert result.headers["X-Schema-Version"] == http.SCHEMA_VERSION
