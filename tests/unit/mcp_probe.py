"""Two probe ops served on both transports, for tests that compare an HTTP answer with
the MCP one for the same request."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import server.auth.bearer as bearer_mod
import server.transport.mcp_call as mcp_call
import server.transport.mcp_outcome as mcp_outcome
from server.auth.jwt import AuthInfo, TokenError
from server.core.errors import NotFoundError
from server.operations.base import Operation, Role
from server.settings import settings
from server.settings.catalog import OP_BUDGETS_P99_MS

TOOL = "loom_code_test_refusal_probe"
PATH = "/v1/code.test_refusal_probe"
FLAT_TOOL = "loom_test_refusal_probe"
FLAT_PATH = "/v1/test.refusal_probe"


class _In(BaseModel):
    org: str = ""
    namespace: str = ""
    agent: str = "-"
    query: str = ""
    limit: int = 5
    missing: bool = False


class _Out(BaseModel):
    ok: bool = True


def _principal(subject: str, org: str) -> AuthInfo:
    return AuthInfo(
        subject=subject, org=org, namespaces=("n",), roles=(Role.WRITER,), is_service_account=True
    )


def serve_probe(monkeypatch: pytest.MonkeyPatch, handler: Any = None) -> dict[str, Any]:
    """Two write ops on both transports, one in the code plane and one outside it. The
    role gate refuses subject "intruder"; the handler raises NotFoundError when asked
    for a missing row."""
    import server.code.app.member_writes as member_writes
    from server.transport import http as http_mod

    async def _handler(inp: Any) -> _Out:
        if inp.missing:
            raise NotFoundError("no such row")
        return _Out()

    def _op(name: str) -> Operation:
        return Operation(
            name=name,
            input_model=_In,
            output_model=_Out,
            handler=handler or _handler,
            idempotent=False,
            required_role=Role.WRITER,
        )

    # A code.* op (the plane's defaults) and one outside the code/tracker planes.
    op, flat = _op("code.test_refusal_probe"), _op("test.refusal_probe")

    async def _authorize_op(auth: Any, op_: Any, **_k: Any) -> None:
        if auth.subject == "intruder":
            raise PermissionError("not a member")

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    tokens = {
        "alice": _principal("alice", org="acme"),
        "intruder": _principal("intruder", org="acme"),
        "orgless": _principal("orgless", org="*"),
    }

    def _verify(token: str) -> AuthInfo:
        if token not in tokens:
            raise TokenError("bad token")
        return tokens[token]

    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-acme")
    monkeypatch.setattr(settings, "tracker_org", "")
    monkeypatch.setattr(member_writes, "authorize_op", _authorize_op)
    monkeypatch.setattr("server.transport.gates._admitted", _noop)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _noop)
    monkeypatch.setattr("server.transport.gates.rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op, flat])
    # Every served op has a latency budget; these two are not in the registry.
    monkeypatch.setitem(OP_BUDGETS_P99_MS, op.name, 10_000)
    monkeypatch.setitem(OP_BUDGETS_P99_MS, flat.name, 10_000)
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(http_mod, "_record_failure", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, TOOL, op)
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, FLAT_TOOL, flat)

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    return {"http": TestClient(app), "tokens": tokens}
