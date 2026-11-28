"""Live document telemetry retains request accounting without the payload."""

from unittest.mock import AsyncMock, Mock

import pytest

from server.core.operation_privacy import KNOWLEDGE_SOURCE_LIVE
from server.observability import ledger


@pytest.mark.parametrize("success", [True, False])
def test_live_ledger_ignores_arguments_results_and_exception_text(monkeypatch, success) -> None:
    dispatch = Mock(return_value=False)
    project = Mock(side_effect=AssertionError("a live payload reached the diagnostic projector"))
    monkeypatch.setattr(ledger, "_dispatch_event", dispatch)
    monkeypatch.setattr("server.observability.event_args.project_args", project)
    monkeypatch.setattr("server.observability.event_args.project_result", project)
    monkeypatch.setattr("server.observability.event_args.bounded_error", project)
    ledger.emit_agent_event(
        op_name=KNOWLEDGE_SOURCE_LIVE,
        identity={
            "org": "test-org",
            "namespace": "docs",
            "agent": "-",
            "user_id": "-",
            "session_id": "-",
        },
        request_id="req-1",
        latency_ms=22,
        success=success,
        error_code="" if success else "INTERNAL_ERROR",
        raw_args={"query": "input-canary", "run_id": "unknown-canary"},
        result={"document": {"text": "result-canary"}} if success else None,
        error=None if success else ValueError("exception-canary"),
    )
    dispatch.assert_called_once()
    event = dispatch.call_args.args[0]
    assert event.request_id == "req-1"
    assert event.operation == KNOWLEDGE_SOURCE_LIVE
    assert event.success is success
    assert event.latency_ms == 22
    assert not event.run_id
    assert not event.response_tokens
    assert "canary" not in str(event.args)


@pytest.mark.asyncio
async def test_live_mcp_capture_ignores_namespace_consent(monkeypatch) -> None:
    from server.observability import capture_consent
    from server.transport.mcp_call import _capture_level

    consent = AsyncMock(return_value="full")
    monkeypatch.setattr(capture_consent, "resolve", consent)
    identity = {"org": "test-org", "namespace": "docs"}
    assert await _capture_level(KNOWLEDGE_SOURCE_LIVE, identity) == "off"
    consent.assert_not_awaited()
    assert await _capture_level("knowledge.search", identity) == "full"
    consent.assert_awaited_once_with("test-org", "docs")


@pytest.mark.asyncio
async def test_registered_live_operation_is_read_only_and_disabled_without_a_directory() -> None:
    from server.knowledge.api.models_source_live import KnowledgeSourceLiveInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.http import build_router

    operation = OPERATIONS_BY_NAME["knowledge.source.live"]
    assert operation.read_only
    assert operation.http_path in {route.path for route in build_router().routes}
    response = await operation.handler(
        KnowledgeSourceLiveInput(
            org="test-org", namespace="docs", source="test", resource_id="mcp://example/test"
        )
    )
    assert response.status == "unavailable"
    assert response.document is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["own_user", "own_session", "pinned"])
async def test_live_read_scope_refuses_at_http_and_mcp_admission(monkeypatch, scope) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from server.auth.jwt import AuthInfo
    from server.knowledge.api.models_source_live import KnowledgeSourceLiveInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates, http, mcp_call

    op = OPERATIONS_BY_NAME[KNOWLEDGE_SOURCE_LIVE]
    claims = {"read_user_id": "alice"} if scope == "pinned" else {"read_scope": scope}
    auth = AuthInfo(
        subject="alice",
        org="test-org",
        namespaces=("docs",),
        roles=(),
        is_service_account=False,
        **claims,
    )
    body = KnowledgeSourceLiveInput(
        org="test-org", namespace="docs", source="test", resource_id="mcp://example/test"
    )
    target = gates.authz_target(op, org=body.org, namespace=body.namespace, auth=auth)
    policy = AsyncMock(side_effect=AssertionError("live read went past read-scope admission"))
    monkeypatch.setattr(gates, "policy", policy)
    monkeypatch.setattr(gates, "traffic_class", AsyncMock(return_value=("agent", "fixture")))
    arguments = body.model_dump()
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    http_call = http._HttpCall(request_id="r", raw_body=arguments, auth=auth)
    with pytest.raises(HTTPException) as refusal:
        await http._bind_context_and_policy(op, request, http_call, target, body)
    assert refusal.value.status_code == 403

    mcp_call_data = mcp_call._AdmittedCall(
        1, op, body, arguments, auth, "r", "", body.namespace, body.org, target
    )
    response = await mcp_call._admission_refusal(mcp_call_data)
    assert response is not None and response["result"]["isError"] is True
    assert "read scope" in response["result"]["content"][0]["text"]
    policy.assert_not_awaited()


def test_direct_live_call_still_refuses_a_narrow_protected_context(monkeypatch) -> None:
    from server.auth.jwt import AuthInfo
    from server.knowledge.app import mcp_worker_protected as protected
    from server.knowledge.app.mcp_worker_read import WorkerReadRefused

    wide = AuthInfo(
        subject="alice", org="test-org", namespaces=("docs",), roles=(), is_service_account=False
    )
    narrow = AuthInfo(
        subject="alice",
        org="test-org",
        namespaces=("docs",),
        roles=(),
        is_service_account=False,
        read_scope="own_session",
    )
    monkeypatch.setattr(type(protected.settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(protected.settings, "knowledge_mirrored_sources_enabled", True)
    monkeypatch.setattr(protected, "protected_for", lambda auth: True)
    monkeypatch.setattr(protected, "current_auth", lambda: wide)
    protected._require_context("test-org", wide)

    monkeypatch.setattr(protected, "current_auth", lambda: narrow)
    with pytest.raises(WorkerReadRefused, match="^denied$"):
        protected._require_context("test-org", narrow)


@pytest.mark.parametrize(
    "req_id, invalid",
    [
        ("x" * 4094, False),
        ("文書📄", False),
        ("x" * 4095, True),
        ("\x01" * 1024, True),
        ([1], True),
    ],
    ids=["near_limit", "unicode", "large", "escaped", "array"],
)
def test_live_mcp_bounds_response_id(req_id, invalid) -> None:
    from server.transport.mcp_op import invalid_live_request_id

    assert (
        invalid_live_request_id(
            {"method": "tools/call", "id": req_id, "params": {"name": "loom_knowledge_source_live"}}
        )
        is invalid
    )


@pytest.mark.asyncio
async def test_live_refuses_unbounded_ignored_argument_names_in_both_transports(
    monkeypatch,
) -> None:
    from fastapi import HTTPException

    from server.knowledge.api.ops_worker_source import knowledge_source_live
    from server.transport import http, mcp_call

    arguments = {"extra-" + "x" * 8192: True}
    monkeypatch.setattr(http, "_emit_event", Mock())
    monkeypatch.setattr(mcp_call, "_emit_refused", Mock())
    request = Mock(json=AsyncMock(return_value=arguments))
    with pytest.raises(HTTPException) as refused:
        await http._read_body(knowledge_source_live, request, "r", None)
    assert refused.value.status_code == 422
    assert len(str(refused.value.detail)) < 512
    response = mcp_call._validated_input(1, knowledge_source_live, arguments, "r")
    assert len(str(response)) < 512
    assert response["result"]["isError"]
    assert "argument names" in str(response)


def _json_request(body: bytes):
    from starlette.requests import Request

    return Request({"type": "http"}, AsyncMock(return_value={"type": "http.request", "body": body}))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["direct", "loom_op"])
@pytest.mark.parametrize("entry", ["single", "batch", "version"])
async def test_live_mcp_refuses_invalid_id_at_the_endpoint(monkeypatch, tool, entry) -> None:
    import httpx
    from fastapi import FastAPI

    from server.transport import mcp
    from server.transport.mcp_config import (
        _TRANSPORT_ERROR_ID,
        INVALID_REQUEST,
        UNSUPPORTED_PROTOCOL_VERSION,
    )

    request_id = 1 << 128
    screen = AsyncMock(return_value={"jsonrpc": "2.0", "id": request_id, "result": {}})
    monkeypatch.setattr(mcp, "_verify_bearer", AsyncMock(return_value=object()))
    monkeypatch.setattr(mcp, "_screen_tool_call", screen)
    arguments = {"namespace": "docs"}
    params = (
        {"name": "loom_knowledge_source_live", "arguments": arguments}
        if tool == "direct"
        else {
            "name": "loom_op",
            "arguments": {"op": "knowledge_source.live", "arguments": arguments},
        }
    )
    request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"MCP-Protocol-Version": "2099-01-01"} if entry == "version" else {}
    app = FastAPI()
    mcp.attach(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        response = await client.post(
            "http://test/mcp",
            json=[request, ping] if entry == "batch" else request,
            headers=headers,
        )

    body = response.json()
    if entry == "batch":
        body, sibling = body
        assert sibling == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert body["id"] == _TRANSPORT_ERROR_ID
    expected = UNSUPPORTED_PROTOCOL_VERSION if entry == "version" else INVALID_REQUEST
    assert body["error"]["code"] == expected
    screen.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_refuses_an_escaped_lone_surrogate_argument_name(monkeypatch) -> None:
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from server.knowledge.api.ops_worker_source import knowledge_source_live
    from server.transport import http, mcp_call

    monkeypatch.setattr(http, "_emit_event", Mock())
    monkeypatch.setattr(mcp_call, "_emit_refused", Mock())
    request = _json_request(
        rb'{"org":"test","namespace":"docs","source":"test",'
        rb'"resource_id":"mcp://example/test","\ud800":true}'
    )
    with pytest.raises(HTTPException) as refusal:
        await http._read_body(knowledge_source_live, request, "r", None)
    assert refusal.value.status_code == 422
    response = mcp_call._validated_input(1, knowledge_source_live, await request.json(), "r")
    assert JSONResponse(response).status_code == 200
    assert response["result"]["isError"]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_live_reports_ignored_names_only_to_the_caller(monkeypatch, caplog, transport) -> None:
    from fastapi.responses import JSONResponse

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import arg_check, lean_results

    monkeypatch.setattr(arg_check, "_NOTED", set())
    caplog.set_level("INFO", logger="server.transport.arg_check")
    op = OPERATIONS_BY_NAME[KNOWLEDGE_SOURCE_LIVE]
    name = "private-note\n東京"
    arguments = {name: True}
    result = {"status": "unavailable"}
    assert arg_check.live_argument_names_fit(op.input_model, arguments)
    if transport == "http":
        body = arg_check.response_body(op, arguments, result)
        assert arg_check.header_value(op, arguments, result) == "private-note%0A%E6%9D%B1%E4%BA%AC"
    else:
        body = lean_results.mcp_op_result(op.name, result, arguments, None)
    assert JSONResponse(body).status_code == 200
    assert body["ignored_args"] == [name]
    assert not arg_check._NOTED
    assert "private-note" not in caplog.text
