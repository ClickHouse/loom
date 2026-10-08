"""Two transport error shapes a client depends on.

1. A scoped token (ns_roles) calling an op that binds no namespace
   (`skip_namespace_authz`) is refused IDENTITY_INCOMPLETE by the role gate.
   HTTP answers 400 IDENTITY_INCOMPLETE; MCP must answer the same code as an
   `isError` tool result inside a JSON-RPC frame — not let the exception
   escape as a bare HTTP 400 that loses the whole batch.
2. An unexpected handler exception never hands its text (ClickHouse / SQL)
   to the client: both transports answer a generic message naming the
   request id and log the exception server-side.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

import server.transport.mcp_call as mcp_call
import server.transport.mcp_outcome as mcp_outcome
from server.auth.jwt import AuthInfo
from server.operations.base import Operation, Role

TOOL = "loom_test_org_level_probe"
LEAK = "Code: 60. DB::Exception: Table loom.secret_table does not exist. SELECT * FROM loom.memory"


class _In(BaseModel):
    org: str = "acme"


class _Out(BaseModel):
    ok: bool = True


def _scoped() -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org="acme",
        namespaces=("priv",),
        roles=(),
        is_service_account=True,
        ns_roles=(("priv", "writer"),),
    )


@pytest.fixture
def org_level_tool(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []

    async def _handler(inp: Any) -> _Out:
        ran.append("ran")
        return _Out()

    op = Operation(
        name="test.org_level_probe",
        input_model=_In,
        output_model=_Out,
        handler=_handler,
        idempotent=True,
        budget_p99_ms=10_000,
        required_role=Role.VIEWER,
        skip_namespace_authz=True,
    )
    monkeypatch.setitem(mcp_call._TOOLS_BY_NAME, TOOL, op)

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    return ran


def test_mcp_answers_identity_incomplete_as_a_tool_result(org_level_tool: list[str]) -> None:
    params = {"name": TOOL, "arguments": {"org": "acme"}}
    out = asyncio.run(mcp_call._call_tool(7, params, auth=_scoped()))
    assert out["id"] == 7 and "error" not in out
    res = out["result"]
    assert res["isError"] is True
    assert res["structuredContent"]["code"] == "IDENTITY_INCOMPLETE"
    assert res["content"][0]["text"].startswith("IDENTITY_INCOMPLETE")
    assert org_level_tool == [], "a refused call never runs the handler"


def test_mcp_internal_error_frame_carries_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)

    class _Op:
        name = "memory.search"

    with caplog.at_level(logging.ERROR):
        out = mcp_outcome._handler_failed(
            RuntimeError(LEAK),
            3,
            "loom_search",
            _Op(),
            {},
            None,
            None,
            "rid-123",
            None,
            rl_ns="ns",
            elapsed_ms=1,
        )
    msg = out["error"]["message"]
    assert "DB::Exception" not in msg and "SELECT" not in msg
    assert "rid-123" in msg
    assert "DB::Exception" in caplog.text, "the full exception is logged server-side"


def test_http_internal_error_detail_carries_no_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import server.transport.http as http

    monkeypatch.setattr(http, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(http, "_record_failure", lambda *a, **k: None)

    async def _boom(inp: Any) -> _Out:
        raise RuntimeError(LEAK)

    op = Operation(
        name="test.boom",
        input_model=_In,
        output_model=_Out,
        handler=_boom,
        idempotent=True,
        budget_p99_ms=10_000,
        required_role=Role.VIEWER,
    )
    call = http._HttpCall(request_id="rid-456", identity={"namespace": "ns"})
    with caplog.at_level(logging.ERROR), pytest.raises(HTTPException) as exc:
        asyncio.run(http._run_handler(op, _In(), call))
    assert exc.value.status_code == 500
    detail = exc.value.detail
    assert detail["code"] == "INTERNAL_ERROR" and detail["request_id"] == "rid-456"
    assert "DB::Exception" not in detail["message"] and "rid-456" in detail["message"]
    assert "DB::Exception" in caplog.text


def test_untrusted_issuer_does_not_list_the_trusted_issuers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt as pyjwt

    from server.auth.jwt import TokenError, verify_token
    from server.settings import settings

    secret = "test-secret-01234567890123456789012345678901"
    monkeypatch.setattr(settings, "jwt_trusted_issuers", "internal-idp.corp.example")
    token = pyjwt.encode(
        {"sub": "x", "iss": "evil", "aud": settings.jwt_audience, "iat": 1, "org": "o"},
        secret,
        algorithm="HS256",
    )
    with pytest.raises(TokenError) as exc:
        verify_token(token, secret=secret)
    text = str(exc.value)
    assert "untrusted issuer" in text
    assert settings.jwt_issuer not in text and "internal-idp" not in text
