"""One cross-org rule on both transports and in the handlers.

`authz.may_act_in_org`: a token of the org, an org-wide (`*`) token, or a
platform admin may act in an org; anyone else is refused without learning
anything about it. HTTP's gate (check_authorized), MCP's pre-dispatch screen
and the handlers' own same-org checks all apply that one rule, so a platform
admin is admitted across orgs on every path.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import server.auth.bearer as bearer_mod
import server.transport.mcp as mcp
import server.transport.mcp_call as mcp_call
import server.transport.mcp_outcome as mcp_outcome
from server.auth.authz import may_act_in_org
from server.auth.jwt import AuthInfo, TokenError
from server.operations.base import Operation, Role
from server.settings import settings
from server.settings.catalog import OP_BUDGETS_P99_MS

TOOL = "loom_test_org_probe"
TOKENS = {
    "platform": AuthInfo(
        subject="ops",
        org="plat",
        namespaces=("*",),
        roles=(Role.PLATFORM_ADMIN,),
        is_service_account=True,
    ),
    "admin-o": AuthInfo(
        subject="ada", org="o", namespaces=("*",), roles=(Role.ADMIN,), is_service_account=True
    ),
    "admin-p": AuthInfo(
        subject="pat", org="p", namespaces=("*",), roles=(Role.ADMIN,), is_service_account=True
    ),
}


class _In(BaseModel):
    org: str


class _Out(BaseModel):
    ok: bool = True


def _verify(token: str) -> AuthInfo:
    if token not in TOKENS:
        raise TokenError("bad token")
    return TOKENS[token]


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An org-level ADMIN op (no namespace) on both transports, the real
    role gate, no admission I/O."""
    from server.transport import http as http_mod
    from server.transport import mcp_tools

    ran: list[str] = []

    async def _handler(inp: Any) -> _Out:
        ran.append(inp.org)
        return _Out()

    op = Operation(
        name="test.org_probe",
        input_model=_In,
        output_model=_Out,
        handler=_handler,
        idempotent=True,
        required_role=Role.ADMIN,
    )

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr("server.transport.gates._admitted", _noop)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _noop)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr(settings, "oauth_enabled", False)
    monkeypatch.setattr(settings, "identity_provider", "", raising=False)
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    monkeypatch.setattr("server.auth.jwt.verify_token", _verify)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setitem(OP_BUDGETS_P99_MS, op.name, 10_000)  # a probe op's budget
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, TOOL, op)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_tools, "_emit_mcp_event", lambda *a, **k: None)
    http_app = FastAPI()
    http_app.include_router(http_mod.build_router())
    mcp_app = FastAPI()
    mcp.attach(mcp_app)
    return {"http": TestClient(http_app), "mcp": TestClient(mcp_app), "ran": ran}


def _http(c: dict[str, Any], who: str, org: str) -> Any:
    return c["http"].post(
        "/v1/test.org_probe", json={"org": org}, headers={"Authorization": f"Bearer {who}"}
    )


def _mcp(c: dict[str, Any], who: str, org: str) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"org": org}},
    }
    r = c["mcp"].post("/mcp", json=body, headers={"Authorization": f"Bearer {who}"})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_a_platform_admin_acts_in_another_org_on_both(clients: dict[str, Any]) -> None:
    assert _http(clients, "platform", "o").status_code == 200
    assert _mcp(clients, "platform", "o")["isError"] is False
    assert clients["ran"] == ["o", "o"]


def test_an_org_admin_acts_in_its_own_org_on_both(clients: dict[str, Any]) -> None:
    assert _http(clients, "admin-o", "o").status_code == 200
    assert _mcp(clients, "admin-o", "o")["isError"] is False


def test_another_orgs_admin_is_refused_on_both_and_learns_nothing(
    clients: dict[str, Any],
) -> None:
    r = _http(clients, "admin-p", "o")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    out = _mcp(clients, "admin-p", "o")
    assert out["isError"] is True and out["structuredContent"]["code"] == "ORG_MISMATCH"
    assert clients["ran"] == []


def test_the_handlers_same_org_checks_ask_the_same_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth.context import set_current_auth
    from server.code.app import repo_admin as code_repo_admin
    from server.core.errors import AuthInsufficientError
    from server.tracker.app import sources as tracker_sources

    for check in (code_repo_admin._require_same_org, tracker_sources._require_same_org):
        set_current_auth(TOKENS["platform"])
        check("o")  # admitted
        set_current_auth(TOKENS["admin-o"])
        check("o")
        set_current_auth(TOKENS["admin-p"])
        with pytest.raises(AuthInsufficientError):
            check("o")
    set_current_auth(None)
    assert may_act_in_org(TOKENS["platform"], "o") and not may_act_in_org(TOKENS["admin-p"], "o")


def test_a_platform_admin_still_meets_the_restricted_gate() -> None:
    """A restricted namespace refuses every other org, platform admins included."""
    import inspect

    from server.auth import authz

    src = inspect.getsource(authz._check_restricted)
    assert "if auth.org != org:\n        raise refusal" in src
    assert asyncio.iscoroutinefunction(authz._check_restricted)
