"""An MCP refusal names the same code HTTP answers for the same bad request.

Claude Code hands the model only an `isError` result's structuredContent, so
the code has to be there for a client to tell a wrong argument from a missing
grant or a missing row. Each case sends one request to both transports: HTTP
answers `detail.code`, MCP answers `structuredContent.code`.
"""

from __future__ import annotations

import asyncio
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


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """One write op on both transports. The role gate refuses subject
    "intruder"; the handler raises NotFoundError when asked for a missing row."""
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
            handler=_handler,
            idempotent=False,
            budget_p99_ms=10_000,
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
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(http_mod, "_record_failure", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    monkeypatch.setitem(mcp_call._TOOLS_BY_NAME, TOOL, op)
    monkeypatch.setitem(mcp_call._TOOLS_BY_NAME, FLAT_TOOL, flat)

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    return {"http": TestClient(app), "tokens": tokens}


def _codes(
    probe: dict[str, Any], who: str, body: dict[str, Any], *, flat: bool = False
) -> tuple[str, str, Any]:
    path, tool = (FLAT_PATH, FLAT_TOOL) if flat else (PATH, TOOL)
    r = probe["http"].post(path, json=body, headers={"Authorization": f"Bearer {who}"})
    assert r.status_code >= 400, r.text
    http_code = r.json()["detail"]["code"]
    params = {"name": tool, "arguments": dict(body)}
    out = asyncio.run(mcp_call._call_tool(1, params, auth=probe["tokens"][who]))
    res = out["result"]
    assert res["isError"] is True and res["content"][0]["text"]
    return http_code, res["structuredContent"]["code"], res["structuredContent"]


def test_input_validation_is_validation_failed_on_both(probe: dict[str, Any]) -> None:
    http_code, mcp_code, body = _codes(probe, "alice", {"namespace": "team", "limit": "many"})
    assert http_code == mcp_code == "VALIDATION_FAILED"
    # additive: the correction parts are still there
    assert body["op"] == "code.test_refusal_probe" and body["errors"]


def test_a_misspelled_write_argument_is_unknown_argument_on_both(probe: dict[str, Any]) -> None:
    http_code, mcp_code, body = _codes(probe, "alice", {"namespace": "team", "qurey": "x"})
    assert http_code == mcp_code == "UNKNOWN_ARGUMENT"
    assert body["did_you_mean"] == {"qurey": "query"}


def test_a_namespace_without_an_org_is_identity_incomplete_on_both(
    probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "code_org", "")
    http_code, mcp_code, _ = _codes(probe, "orgless", {"namespace": "code-x"})
    assert http_code == mcp_code == "IDENTITY_INCOMPLETE"


def test_a_role_refusal_is_auth_insufficient_on_both(
    probe: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.transport import gates

    async def _refuse(*_a: Any, **_k: Any) -> None:
        raise PermissionError("not a member")

    monkeypatch.setattr(gates, "authorize", _refuse)
    body = {"org": "acme", "namespace": "team"}
    http_code, mcp_code, _ = _codes(probe, "intruder", body, flat=True)
    assert http_code == mcp_code == "AUTH_INSUFFICIENT"


def test_a_code_index_role_refusal_is_one_code_with_its_reason_on_both(
    probe: dict[str, Any],
) -> None:
    """A code/tracker op refused by the role gate answers AUTH_INSUFFICIENT on
    both transports, with NO_REPO_ACCESS as the reason and retry false."""
    body = {"org": "acme", "namespace": "team"}
    r = probe["http"].post(PATH, json=body, headers={"Authorization": "Bearer intruder"})
    http_detail = r.json()["detail"]
    http_code, mcp_code, mcp_body = _codes(probe, "intruder", body)
    assert r.status_code == 403 and http_code == mcp_code == "AUTH_INSUFFICIENT"
    for got in (http_detail, mcp_body):
        assert (got["reason"], got["namespace"], got["retry"]) == ("NO_REPO_ACCESS", "team", False)


def test_a_missing_row_is_resource_not_found_on_both(probe: dict[str, Any]) -> None:
    http_code, mcp_code, _ = _codes(
        probe, "alice", {"org": "acme", "namespace": "team", "missing": True}
    )
    assert http_code == mcp_code == NotFoundError.code


def test_an_unknown_tool_is_unknown_op_as_through_loom_op() -> None:
    from server.transport.mcp_op import route

    out = asyncio.run(mcp_call._call_tool(1, {"name": "loom_no_such_tool", "arguments": {}}))
    body = out["result"]["structuredContent"]
    assert body["code"] == "UNKNOWN_OP" and body["tool"] == "loom_no_such_tool"
    req = {"id": 1, "method": "tools/call", "params": {"name": "loom_op", "arguments": {}}}
    req["params"]["arguments"] = {"op": "no.such_op"}
    _, via_op = route(req, toolset=None)
    assert via_op is not None and via_op["result"]["structuredContent"]["code"] == body["code"]
