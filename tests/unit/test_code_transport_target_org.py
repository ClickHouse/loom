"""Code handlers receive the organization authorized by either transport."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token, verify_token
from server.code.app.common import resolve_code_identity
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.transport import http as http_mod
from server.transport import mcp_call, mcp_outcome
from tests.unit.ch_fake import RecordingCH

INPUTS = {
    "code.symbol": {"name": "Parser.parse"},
    "code.callers": {"name": "Parser.parse"},
    "code.search": {"query": "parse a record"},
    "code.impact": {"name": "Parser.parse"},
}
OUTPUTS = {
    "code.symbol": {"definitions": []},
    "code.callers": {"edges": [], "seed_symbols": []},
    "code.search": {"results": [], "route": "hybrid"},
    "code.impact": {"impacted_symbols": [], "impacted_tests": [], "co_change_files": []},
}


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[tuple[str, str, str]]]:
    seen: list[tuple[str, str, str]] = []
    ops = []
    for name in INPUTS:
        base = OPERATIONS_BY_NAME[name]

        async def handler(inp: Any, op: Any = base) -> Any:
            org, namespace = resolve_code_identity(inp.org, inp.namespace)
            seen.append((op.name, org, namespace))
            return op.output_model(**OUTPUTS[op.name])

        op = dataclasses.replace(base, handler=handler)
        ops.append(op)
        monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, op.mcp_tool_name, op)

    async def defaults(auth: Any, client_name: str | None = None) -> dict[str, str] | None:
        if auth is None or auth.org == "*":
            return None
        return {"org": auth.org, "namespace": "personal-reader", "agent": "reader"}

    RecordingCH(strict=False).install(monkeypatch)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "code_org", "")
    monkeypatch.setattr(settings, "code_namespace", "")
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    monkeypatch.setattr(mcp_call, "_caller_defaults", defaults)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    return TestClient(app, raise_server_exceptions=False), seen


def token(org: str = "acme", namespaces: list[str] | None = None) -> str:
    return mint_token(
        subject="reader",
        org=org,
        namespaces=namespaces or ["code-repo"],
        roles=["viewer"],
        ttl_seconds=300,
    )


def call(transport: Any, kind: str, op: str, body: dict[str, Any], credential: str | None) -> Any:
    if kind == "http":
        return transport.post(
            f"/v1/{op}",
            json=body,
            headers={"Authorization": f"Bearer {credential}"} if credential else {},
        )
    params = {"name": OPERATIONS_BY_NAME[op].mcp_tool_name, "arguments": body}
    auth = verify_token(credential) if credential else None
    return asyncio.run(mcp_call._call_tool(1, params, auth=auth))


@pytest.mark.parametrize("kind", ["http", "mcp"])
@pytest.mark.parametrize("op", INPUTS)
def test_an_omitted_org_uses_the_authorized_token_org(transport: Any, kind: str, op: str) -> None:
    client, seen = transport
    response = call(client, kind, op, {"namespace": "code-repo", **INPUTS[op]}, token())
    assert seen == [(op, "acme", "code-repo")], response
    if kind == "http":
        assert response.status_code == 200, response.text
    else:
        assert response["result"]["isError"] is False, response


@pytest.mark.parametrize("kind", ["http", "mcp"])
def test_a_code_request_keeps_the_deployment_org_precedence(
    transport: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    client, seen = transport
    monkeypatch.setattr(settings, "code_org", "deployment-org")
    response = call(
        client,
        kind,
        "code.symbol",
        {"namespace": "code-repo", "name": "Parser.parse"},
        token(org="*", namespaces=["*"]),
    )
    assert seen == [("code.symbol", "deployment-org", "code-repo")], response


@pytest.mark.parametrize("kind", ["http", "mcp"])
def test_an_explicit_authorized_org_wins_over_the_deployment_default(
    transport: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    client, seen = transport
    monkeypatch.setattr(settings, "code_org", "deployment-org")
    response = call(
        client,
        kind,
        "code.symbol",
        {"org": "chosen-org", "namespace": "code-repo", "name": "Parser.parse"},
        token(org="*", namespaces=["*"]),
    )
    assert seen == [("code.symbol", "chosen-org", "code-repo")], response


@pytest.mark.parametrize("kind", ["http", "mcp"])
@pytest.mark.parametrize(
    "body,org,namespaces",
    [
        ({"namespace": "code-other"}, "acme", ["code-repo"]),
        ({"org": "other-org", "namespace": "code-repo"}, "acme", ["code-repo"]),
        ({"namespace": "code-repo"}, "*", ["*"]),
    ],
)
def test_an_unresolvable_or_unauthorized_target_never_reaches_the_handler(
    transport: Any, kind: str, body: dict[str, Any], org: str, namespaces: list[str]
) -> None:
    client, seen = transport
    response = call(
        client, kind, "code.symbol", {**body, "name": "Parser.parse"}, token(org, namespaces)
    )
    assert seen == []
    if kind == "http":
        assert response.status_code in (400, 403), response.text
    else:
        assert response["result"]["isError"] is True, response
        assert response["result"]["structuredContent"]["code"] in (
            "AUTH_INSUFFICIENT",
            "IDENTITY_INCOMPLETE",
        )


@pytest.mark.parametrize("kind", ["http", "mcp"])
def test_an_auth_disabled_request_keeps_the_deployment_identity(
    transport: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    client, seen = transport
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    monkeypatch.setattr(settings, "code_org", "deployment-org")
    response = call(
        client, kind, "code.symbol", {"namespace": "code-repo", "name": "Parser.parse"}, None
    )
    assert seen == [("code.symbol", "deployment-org", "code-repo")], response


@pytest.mark.parametrize("namespace", [None, "code-repo"])
def test_the_header_never_replaces_an_explicit_namespace(
    transport: Any, namespace: str | None
) -> None:
    from server.code.domain.scope import reset_code_scope, set_code_scope

    client, seen = transport
    body = {"name": "Parser.parse"}
    if namespace:
        body["namespace"] = namespace
    scope = set_code_scope("acme/code-repo" if namespace is None else "other-org/code-other")
    try:
        response = call(client, "mcp", "code.symbol", body, token())
    finally:
        reset_code_scope(scope)
    assert seen == [("code.symbol", "acme", "code-repo")], response


def test_an_unauthorized_header_target_is_refused(transport: Any) -> None:
    from server.code.domain.scope import reset_code_scope, set_code_scope

    client, seen = transport
    scope = set_code_scope("other-org/code-repo")
    try:
        response = call(client, "mcp", "code.symbol", {"name": "Parser.parse"}, token())
    finally:
        reset_code_scope(scope)
    assert seen == []
    assert response["result"]["isError"] is True, response


def test_required_identity_fields_are_not_filled_by_the_target() -> None:
    from pydantic import BaseModel

    from server.transport.gates import Target, with_target

    class RequiredIdentity(BaseModel):
        org: str
        namespace: str

    inp = RequiredIdentity(org="", namespace="")
    target = Target("acme", "code-repo", None, "code-repo")
    assert with_target(OPERATIONS_BY_NAME["code.symbol"], inp, target) is inp


@pytest.mark.parametrize("kind", ["http", "mcp"])
@pytest.mark.parametrize(
    "name,fields,answer",
    [
        ("code.outcome", {"action": "shunt_hit"}, {"recorded": True}),
        (
            "code.scip_ingest",
            {
                "batch_id": "00000000-0000-4000-8000-000000000001",
                "seq": 0,
                "total_parts": 1,
                "payload": "{}",
            },
            {
                "batch_id": "00000000-0000-4000-8000-000000000001",
                "parts_received": 1,
                "total_parts": 1,
                "complete": False,
            },
        ),
        (
            "code.coverage.watch",
            {"commit": "a" * 40, "inbox": "builds"},
            {
                "org": "acme",
                "namespace": "code-repo",
                "commit": "a" * 40,
                "inbox": "builds",
                "event_id": "ready",
            },
        ),
        (
            "code.coverage.unwatch",
            {"commit": "a" * 40, "inbox": "builds"},
            {"commit": "a" * 40, "inbox": "builds", "removed": True},
        ),
    ],
)
def test_writes_preserve_pre_upgrade_replays_and_hidden_mcp_routes(
    transport: Any,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    name: str,
    fields: dict[str, Any],
    answer: dict[str, Any],
) -> None:
    from server.core import idempotency

    client, _ = transport
    op = OPERATIONS_BY_NAME[name]
    assert op.idempotent and not op.read_only
    body = {"namespace": "code-repo", **fields}
    legacy = op.input_model.model_validate(body)
    assert legacy.org == ""
    legacy_hash = idempotency.request_hash(legacy)
    cached = op.output_model.model_validate(answer).model_dump(mode="json")
    checks: list[str] = []

    async def check(key: str, body_hash: str) -> idempotency.CacheResult:
        assert key == idempotency.scoped_key("retry", org="acme", subject="reader", operation=name)
        checks.append(body_hash)
        hit = idempotency.CacheHit(200, cached, {}) if body_hash == legacy_hash else None
        return idempotency.CacheResult(hit=hit, conflict=hit is None)

    async def never_write(inp: Any) -> Any:
        pytest.fail("an existing idempotency receipt must replay without the handler")

    op = dataclasses.replace(op, handler=never_write)
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, op.mcp_tool_name, op)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(idempotency, "check", check)
    credential = mint_token(
        subject="reader", org="acme", namespaces=["code-repo"], roles=["admin"], ttl_seconds=300
    )
    if kind == "http":
        app = FastAPI()
        app.include_router(http_mod.build_router())
        response = TestClient(app).post(
            f"/v1/{name}",
            json=body,
            headers={"Authorization": f"Bearer {credential}", "Idempotency-Key": "retry"},
        )
        assert response.status_code == 200, response.text
        assert response.headers.get("X-Idempotent-Replay") == "true"
    else:
        response = call(client, kind, name, {**body, "_idempotency_key": "retry"}, credential)
        assert response["result"]["isError"] is True, response
        assert response["result"]["structuredContent"]["code"] == "OP_NOT_ON_MCP"
    assert checks == ([legacy_hash] if kind == "http" else [])
