"""server/clients/_http_errors.py — a provider's 4xx keeps the body's reason,
and the HTTP transport's HTTPException row names its status
and code."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from server.clients._http_errors import (
    provider_error_code,
    provider_error_detail,
    raise_for_status_with_detail,
)
from server.observability.event_args import bounded_error

_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _openai_400() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": {
                "message": "Invalid prompt: your prompt was flagged as potentially violating "
                "our usage policy.",
                "type": "invalid_request_error",
                "param": None,
                "code": "invalid_prompt",
            }
        },
        request=_REQ,
    )


def test_openai_body_reason_leads_the_message_and_survives_redaction() -> None:
    with pytest.raises(httpx.HTTPStatusError) as ei:
        raise_for_status_with_detail(_openai_400(), "openai:chat")
    exc = ei.value
    first = str(exc).splitlines()[0]
    assert first.startswith("400 Bad Request from openai:chat: ")
    assert "code=invalid_prompt" in first and "type=invalid_request_error" in first
    # Same exception class, request and response: retry / outage predicates
    # keep reading the status code.
    assert exc.response.status_code == 400 and exc.request is _REQ
    # The original httpx line is still there for the log.
    assert "Client error '400 Bad Request'" in str(exc)
    # The ledger keeps the unquoted fields (quoted spans are redacted).
    cls, msg = bounded_error(exc)
    assert cls == "HTTPStatusError" and "invalid_prompt" in msg


def test_anthropic_shape_is_read_too() -> None:
    resp = httpx.Response(
        400,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "prompt is too long: 250k"},
        },
        request=_REQ,
    )
    assert provider_error_detail(resp) == (
        "type=invalid_request_error msg=prompt is too long: 250k"
    )
    with pytest.raises(httpx.HTTPStatusError) as ei:
        raise_for_status_with_detail(resp, "anthropic:messages")
    assert provider_error_code(ei.value) == "invalid_request_error"


def test_non_json_body_raises_the_original_message() -> None:
    resp = httpx.Response(502, text="<html>bad gateway</html>", request=_REQ)
    assert provider_error_detail(resp) == ""
    with pytest.raises(httpx.HTTPStatusError) as ei:
        raise_for_status_with_detail(resp, "openai:chat")
    assert str(ei.value).startswith("Server error '502 Bad Gateway'")
    assert provider_error_code(ei.value) == ""


def test_success_does_not_raise_and_detail_is_bounded() -> None:
    raise_for_status_with_detail(httpx.Response(200, json={"ok": 1}, request=_REQ), "x")
    long = httpx.Response(400, json={"error": {"message": "m" * 500, "code": "c"}}, request=_REQ)
    detail = provider_error_detail(long)
    assert len(detail) <= 160 and detail.startswith("code=c msg=") and detail.endswith("…")


def test_provider_error_code_is_empty_for_other_exceptions() -> None:
    assert provider_error_code(ValueError("x")) == ""
    assert provider_error_code(httpx.ConnectTimeout("t")) == ""


# ---------------------------------------------------------------------------
# HTTP transport: a handler's HTTPException row names status + code.
# ---------------------------------------------------------------------------


class _In(BaseModel):
    org: str = "-"
    namespace: str = "-"


class _Out(BaseModel):
    ok: bool = True


async def _forbidden(_: _In) -> _Out:
    raise HTTPException(
        status_code=403,
        detail={"code": "AUTH_INSUFFICIENT", "message": "principal 'x' cannot act in org 'y'"},
    )


def test_http_exception_row_names_status_and_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.operations.base import Operation, Role
    from server.transport import http as http_mod

    op = Operation(
        name="probe.http_error",
        input_model=_In,
        output_model=_Out,
        handler=_forbidden,
        idempotent=False,
        budget_p99_ms=100,
        required_role=Role.VIEWER,
    )
    seen: list[dict[str, Any]] = []

    def _capture(*_: Any, **kw: Any) -> None:
        seen.append(kw)

    monkeypatch.setattr(http_mod, "_emit_event", _capture)
    monkeypatch.setattr(http_mod.settings, "auth_required", False, raising=False)
    monkeypatch.setattr(
        type(http_mod.settings), "auth_actually_required", lambda self: False, raising=False
    )
    app = FastAPI()
    app.add_api_route(op.http_path, http_mod._make_endpoint(op), methods=["POST"])
    resp = TestClient(app, raise_server_exceptions=False).post("/v1/probe.http_error", json={})

    assert resp.status_code == 403
    rows = [kw for kw in seen if kw.get("error_code") == "HANDLER_HTTP_ERROR"]
    assert rows, seen
    cls, msg = bounded_error(rows[0]["error"])
    assert cls == "HTTPException" and msg == "403: AUTH_INSUFFICIENT"
