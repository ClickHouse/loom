"""An MCP refusal names the same code HTTP answers for the same bad request.

Claude Code hands the model only an `isError` result's structuredContent, so
the code has to be there for a client to tell a wrong argument from a missing
grant or a missing row. Each case sends one request to both transports: HTTP
answers `detail.code`, MCP answers `structuredContent.code`; an unavailable
backend (503) answers over MCP as a JSON-RPC error whose `data.code` is HTTP's.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.transport.mcp_call as mcp_call
from server.core.errors import NotFoundError
from server.settings import settings
from tests.unit.mcp_probe import FLAT_PATH, FLAT_TOOL, PATH, TOOL, serve_probe


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return serve_probe(monkeypatch)


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
    _, via_op = route(req, toolset=None, auth=None)
    assert via_op is not None and via_op["result"]["structuredContent"]["code"] == body["code"]


def test_a_registry_read_between_gate_and_handler_never_defaults_the_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate found the registry unread and checked the org only; the read that lands
    before the handler runs must not let the handler default to an unchecked namespace."""
    from server.code.app.common import resolve_code_identity
    from server.code.domain.index import repos

    reached: list[str] = []
    defaulted: list[tuple[str, str]] = []

    async def _defaults(inp: Any) -> Any:
        reached.append("handler")
        monkeypatch.setattr(repos, "_registry_loaded", True)  # the read lands meanwhile
        defaulted.append(resolve_code_identity(inp.org, inp.namespace))
        return None

    probe = serve_probe(monkeypatch, handler=_defaults)
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    monkeypatch.setattr(repos, "_registry_loaded", False)
    r = probe["http"].post(PATH, json={}, headers={"Authorization": "Bearer alice"})
    assert r.status_code == 503, r.text
    monkeypatch.setattr(repos, "_registry_loaded", False)
    out = asyncio.run(
        mcp_call._call_tool(1, {"name": TOOL, "arguments": {}}, auth=probe["tokens"]["alice"])
    )
    # A JSON-RPC error, as for any unavailable backend, with the code HTTP answers.
    assert r.json()["detail"]["code"] == out["error"]["data"]["code"] == "BACKEND_UNAVAILABLE"
    assert reached == ["handler", "handler"] and defaulted == []
