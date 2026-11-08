"""Every error body carries `category` and `retryable`, on HTTP and MCP alike, so a
caller can decide to retry, fix its request or stop without knowing the code."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import re
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server.core.errors import (
    AuthInsufficientError,
    BackendTimeoutError,
    OverloadedError,
    SchemaPendingError,
    classified,
    error_class,
    handler_failure_detail,
    register_exception_handler,
)
from server.core.principals import personal_name
from server.transport import mcp
from server.transport.mcp_refusals import classify_tool_errors
from tests.unit.mcp_probe import FLAT_PATH, FLAT_TOOL, PATH, TOOL, serve_probe


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return serve_probe(monkeypatch)


# Someone else's personal collection, named the way the gate reads it.
PERSONAL_NS = personal_name("dev", "someone-else")


@pytest.mark.parametrize(
    ("code", "status", "want"),
    [
        ("VALIDATION_FAILED", 422, ("input", False)),
        ("PRECONDITION_FAILED", 409, ("input", False)),
        ("LOCKED", 423, ("input", False)),
        ("AUTH_INVALID", 401, ("auth", False)),
        ("AUTH_INSUFFICIENT", 403, ("auth", False)),
        ("RATE_LIMITED", 429, ("capacity", True)),
        ("OVERLOADED", 503, ("capacity", True)),
        ("IDEMPOTENT_IN_PROGRESS", 503, ("capacity", True)),
        ("BACKEND_UNAVAILABLE", 503, ("dependency", True)),
        ("EMBED_TIMEOUT", 503, ("dependency", True)),
        ("BACKEND_TIMEOUT", 504, ("dependency", True)),
        ("SCHEMA_PENDING", 503, ("configuration", False)),
        ("GATEWAY_REQUIRED", 503, ("configuration", False)),
        ("INTERNAL_ERROR", 500, ("internal", False)),
        ("IDEMPOTENT_TAKEN_OVER", 503, ("capacity", True)),
        ("ADMISSION_CONTENDED", 503, ("capacity", True)),
        ("PROJECTION_BEHIND", 503, ("capacity", True)),
        ("PUBLICATION_PENDING", 503, ("dependency", False)),
        ("POSTGRES_UNAVAILABLE", 503, ("dependency", True)),
        ("POSTGRES_REFUSED", 500, ("internal", False)),
        # An MCP refusal carries no status: the code alone decides.
        ("RATE_LIMITED", 0, ("capacity", True)),
        ("AUTH_INSUFFICIENT", 0, ("auth", False)),
        ("NAMESPACE_NOT_ACCESSIBLE", 0, ("auth", False)),
        ("UNKNOWN_OP", 0, ("input", False)),
    ],
)
def test_error_class(code: str, status: int, want: tuple[str, bool]) -> None:
    assert error_class(code, status) == want


def test_classified_keeps_the_class_an_error_set_itself() -> None:
    assert classified({"code": "LOCKED", "message": "m"}, 423)["retryable"] is False
    own = classified(
        {"code": "AUTH_INSUFFICIENT", "category": "dependency", "retryable": True}, 403
    )
    assert (own["category"], own["retryable"]) == ("dependency", True)


def test_the_sdk_names_the_server_categories() -> None:
    from loom import errors as sdk_errors

    from server.core import errors as server_errors

    assert get_args(sdk_errors.ErrorCategory) == get_args(server_errors.ErrorCategory)


def test_the_typescript_sdk_names_the_same_categories_and_never_retried_codes() -> None:
    from loom import errors as sdk_errors

    from server.core import errors as server_errors

    ts = (pathlib.Path(__file__).parents[2] / "sdk/typescript/src/errors.ts").read_text()
    categories = re.search(r"const CATEGORIES = \[(.*?)\] as const", ts, re.S)
    never = re.search(r"NEVER_RETRIED: .*?new Map<string, ErrorCategory>\(\[(.*?)\]\);", ts, re.S)
    assert categories and never
    assert tuple(re.findall(r'"(\w+)"', categories[1])) == get_args(server_errors.ErrorCategory)
    pairs = dict(re.findall(r'\["(\w+)", "(\w+)"\]', never[1]))
    assert pairs == dict(sdk_errors._NEVER_RETRIED)


def _client() -> TestClient:
    app = FastAPI()
    register_exception_handler(app)

    @app.get("/typed/{which}")
    def typed(which: str) -> None:
        raise {
            "overloaded": OverloadedError("busy"),
            "timeout": BackendTimeoutError("slow", statement_class="read", timeout_s=30),
            "pending": SchemaPendingError("later", table="t", migration="001"),
            "refused": AuthInsufficientError("no grant"),
        }[which]

    @app.get("/coded")
    def coded() -> None:
        raise HTTPException(status_code=423, detail={"code": "LOCKED", "message": "m"})

    @app.get("/plain")
    def plain() -> None:
        raise HTTPException(status_code=404, detail="no such thing")

    return TestClient(app)


@pytest.mark.parametrize(
    ("which", "status", "category", "retryable"),
    [
        ("overloaded", 503, "capacity", True),
        ("timeout", 504, "dependency", True),
        ("pending", 503, "configuration", False),
        ("refused", 403, "auth", False),
    ],
)
def test_a_typed_http_error_carries_its_class(
    which: str, status: int, category: str, retryable: bool
) -> None:
    r = _client().get(f"/typed/{which}")
    detail = r.json()["detail"]
    assert r.status_code == status
    assert (detail["category"], detail["retryable"]) == (category, retryable)


def test_a_coded_http_exception_carries_its_class_and_a_plain_one_is_untouched() -> None:
    c = _client()
    detail = c.get("/coded").json()["detail"]
    assert (detail["code"], detail["category"], detail["retryable"]) == ("LOCKED", "input", False)
    assert c.get("/plain").json() == {"detail": "no such thing"}


def test_a_jsonrpc_fault_carries_its_class() -> None:
    named = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": "m", "data": {"code": "OVERLOADED", "status": 503}},
    }
    data = classify_tool_errors(named)["error"]["data"]
    assert (data["category"], data["retryable"]) == ("capacity", True)
    # Data that names no code is not classed as some fault it may not be.
    unnamed = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "m", "data": {}}}
    assert classify_tool_errors(unnamed) == unnamed
    bare = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}}
    assert classify_tool_errors(bare) == bare


def test_an_mcp_role_refusal_carries_its_class(
    probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import gates

    async def _refuse(*_a: Any, **_k: Any) -> None:
        raise PermissionError("not a member")

    monkeypatch.setattr(gates, "authorize", _refuse)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": FLAT_TOOL, "arguments": {"org": "acme", "namespace": "team"}},
    }
    out = asyncio.run(mcp._handle_request(payload, auth=probe["tokens"]["intruder"]))
    assert out is not None
    body = out["result"]["structuredContent"]
    assert out["result"]["isError"] is True
    assert (body["code"], body["category"], body["retryable"]) == (
        "AUTH_INSUFFICIENT",
        "auth",
        False,
    )
    assert "next" not in body  # a role shortfall: the namespace is not the problem


def test_a_code_index_refusal_gets_no_namespace_step(
    probe: dict[str, Any],
) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": TOOL, "arguments": {"org": "acme", "namespace": "team"}},
    }
    out = asyncio.run(mcp._handle_request(payload, auth=probe["tokens"]["intruder"]))
    assert out is not None
    body = out["result"]["structuredContent"]
    assert (body["reason"], body["retryable"]) == ("NO_REPO_ACCESS", False)
    assert "next" not in body  # its message says what to do instead


def _app_with(http_mod: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(http_mod.build_router())
    return app


def _http(fixture: dict[str, Any]) -> TestClient:
    """The probe's app with Loom's exception handlers, as server.main installs them."""
    client: TestClient = fixture["http"]
    register_exception_handler(client.app)  # type: ignore[arg-type]
    return client


def test_a_refusal_whose_check_could_not_run_is_retryable(
    probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import gates

    async def _refuse(*_a: Any, **_k: Any) -> None:
        e = PermissionError("grants could not be read")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    monkeypatch.setattr(gates, "authorize", _refuse)
    r = _http(probe).post(
        FLAT_PATH,
        json={"org": "acme", "namespace": "team"},
        headers={"Authorization": "Bearer intruder"},
    )
    detail = r.json()["detail"]
    assert (detail["category"], detail["retryable"]) == ("dependency", True)
    assert "next" not in detail


@pytest.mark.parametrize(("idempotent", "retryable"), [(True, True), (False, False)])
@pytest.mark.parametrize(
    ("error", "category"), [("down", "dependency"), ("busy", "capacity"), ("queue", "dependency")]
)
def test_a_handler_5xx_is_retryable_only_for_an_idempotent_op(
    probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    idempotent: bool,
    retryable: bool,
    error: str,
    category: str,
) -> None:
    """OVERLOADED too: each statement waits for a pool slot, so it may follow a write."""

    from server.core.errors import BackendUnavailableError
    from server.transport import http as http_mod

    async def _down(_inp: Any) -> Any:
        if error == "busy":
            raise OverloadedError("busy")
        if error == "queue":  # a handler's own coded 503
            raise HTTPException(
                503, detail={"code": "EXTRACTION_QUEUE_UNAVAILABLE", "message": "m"}
            )
        raise BackendUnavailableError("down", operation="probe")

    ops = [
        dataclasses.replace(o, handler=_down, idempotent=idempotent)
        if o.name == "test.refusal_probe"
        else o
        for o in http_mod.ALL_OPERATIONS
    ]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    probe["http"] = TestClient(_app_with(http_mod))
    r = _http(probe).post(
        FLAT_PATH,
        json={"org": "acme", "namespace": "team"},
        headers={"Authorization": "Bearer alice"},
    )
    detail = r.json()["detail"]
    assert (r.status_code, detail["category"], detail["retryable"]) == (503, category, retryable)


def test_an_overload_before_the_handler_stays_retryable_on_any_op(
    probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auth gate's OVERLOADED comes before the op runs, so resending is safe even on
    an op that is not idempotent."""

    from server.transport import gates
    from server.transport import http as http_mod

    async def _busy(*_a: Any, **_k: Any) -> None:
        raise OverloadedError("busy")

    ops = [
        dataclasses.replace(o, idempotent=False) if o.name == "test.refusal_probe" else o
        for o in http_mod.ALL_OPERATIONS
    ]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    monkeypatch.setattr(gates, "authorize", _busy)
    probe["http"] = TestClient(_app_with(http_mod))
    r = _http(probe).post(
        FLAT_PATH,
        json={"org": "acme", "namespace": "team"},
        headers={"Authorization": "Bearer alice"},
    )
    detail = r.json()["detail"]
    assert (r.status_code, detail["code"], detail["retryable"]) == (503, "OVERLOADED", True)


def test_the_mcp_screen_refusal_carries_its_class_and_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.jwt import mint_token
    from server.settings import settings
    from server.transport import mcp_auth

    async def _no(*_a: Any) -> tuple[bool, bool]:
        return False, False

    monkeypatch.setattr(mcp_auth, "_may_read", _no)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "")
    monkeypatch.setattr(settings, "mcp_auth_mode", "required")
    monkeypatch.setattr("server.transport.mcp_auth._ledger_refusal", lambda *a, **k: None)
    app = FastAPI()
    mcp.attach(app)
    tok = mint_token(
        subject="viewer@dev", org="dev", namespaces=["alpha"], roles=["viewer"], ttl_seconds=300
    )
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "loom_memory_get",
            "arguments": {"namespace": "alhpa", "memory_key": "k"},
        },
    }
    r = TestClient(app).post("/mcp", json=call, headers={"Authorization": f"Bearer {tok}"})
    result = r.json()["result"]
    body = result["structuredContent"]
    assert (body["code"], body["category"], body["retryable"]) == (
        "NAMESPACE_NOT_ACCESSIBLE",
        "auth",
        False,
    )
    from server.operations.registry import OPERATIONS_BY_NAME

    text = result["content"][0]["text"]
    assert OPERATIONS_BY_NAME["namespace.list_mine"].mcp_tool_name in body["next"]
    # The step comes before the grant advice it tells the caller to check first.
    assert text.index("Next:") < text.index("request a grant from")
    # The class, in words, for a host that shows the model only the text.
    assert text.endswith("(auth: do not retry unchanged)")

    # In a JSON-RPC batch each entry is classified on its own.
    bad = {**call, "id": 2, "params": {"name": "loom_memory_get", "arguments": {"memory_key": 1}}}
    r = TestClient(app).post("/mcp", json=[call, bad], headers={"Authorization": f"Bearer {tok}"})
    bodies = {e["id"]: e["result"]["structuredContent"] for e in r.json()}
    assert bodies[1]["next"] == body["next"]
    assert bodies[2]["code"] and bodies[2]["code"] != bodies[1]["code"]
    assert all({"category", "retryable"} <= set(b) for b in bodies.values()), bodies


def test_a_code_index_refusal_over_mcp_is_the_same_whether_or_not_the_check_ran() -> None:
    """A restricted code namespace is refused before the grant read that fails, so a
    different answer for an unverified refusal would tell it from a missing one."""

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_auth, mcp_call

    auth = SimpleNamespace(subject="sa:agent-x", org="acme")
    failed = PermissionError("grants could not be read")
    failed.unverified = True  # type: ignore[attr-defined]
    checked = PermissionError("no grant")
    op = OPERATIONS_BY_NAME["code.search"]
    assert mcp_call._forbidden_result(op, failed, auth, "acme", "code-x") == (
        mcp_call._forbidden_result(op, checked, auth, "acme", "code-x")
    )
    tool = "loom_code_grep"
    assert mcp_auth._not_accessible_result(tool, auth, "code-x", unverified=True) == (
        mcp_auth._not_accessible_result(tool, auth, "code-x")
    )


def test_an_unverified_refusal_on_a_code_index_is_retryable_over_http(
    probe: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import gates

    async def _refuse(*_a: Any, **_k: Any) -> None:
        e = PermissionError("grants could not be read")
        e.unverified = True  # type: ignore[attr-defined]
        raise e

    monkeypatch.setattr(gates, "authorize", _refuse)
    r = _http(probe).post(
        PATH,
        json={"org": "acme", "namespace": "team"},
        headers={"Authorization": "Bearer intruder"},
    )
    detail = r.json()["detail"]
    assert (detail["reason"], detail["retry"], detail["retryable"]) == (
        "ACCESS_UNVERIFIED",
        True,
        True,
    )


@pytest.mark.parametrize(("idempotent", "retryable"), [(True, True), (False, False)])
@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("BACKEND_UNAVAILABLE", "dependency"),
        ("OVERLOADED", "capacity"),
        # A handler's coded HTTPException is classed as over HTTP, not as an internal error.
        ("EXTRACTION_QUEUE_UNAVAILABLE", "dependency"),
    ],
)
def test_a_named_5xx_over_mcp_is_retryable_only_for_an_idempotent_op(
    idempotent: bool, retryable: bool, code: str, category: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import BackendUnavailableError
    from server.transport import mcp_outcome

    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)

    op = SimpleNamespace(name="memory.set", idempotent=idempotent)
    errors: dict[str, Exception] = {
        "BACKEND_UNAVAILABLE": BackendUnavailableError("down", operation="probe"),
        "OVERLOADED": OverloadedError("busy"),
        "EXTRACTION_QUEUE_UNAVAILABLE": HTTPException(503, detail={"code": code, "message": "m"}),
    }
    error = errors[code]
    out = mcp_outcome._handler_failed(
        error,
        9,
        "loom_memory_set",
        op,
        {},
        None,
        None,
        "rid",
        "",
        rl_ns=None,
        elapsed_ms=1,
    )
    data = classify_tool_errors(out)["error"]["data"]
    assert (data["code"], data["category"], data["retryable"]) == (code, category, retryable)


@pytest.mark.parametrize(
    ("state", "code", "category", "retryable"),
    [
        ("drift", "CONFIG_DRIFT", "capacity", True),
        ("rebuild", "MODEL_CHANGE_IN_PROGRESS", "capacity", True),
        ("refused", "GATEWAY_REQUIRED", "configuration", False),
    ],
)
def test_the_maintenance_middleware_answers_carry_their_class(
    monkeypatch: pytest.MonkeyPatch, state: str, code: str, category: str, retryable: bool
) -> None:
    from starlette.applications import Starlette

    from server.core import deployment_config
    from server.core.model_work_gate import ModelWorkGate
    from server.transport.model_maintenance import ModelMaintenanceMiddleware

    monkeypatch.setattr(deployment_config, "drift", lambda: state == "drift")
    gate = ModelWorkGate()
    if state != "drift":
        gate.close("t", refusal="off the gateway" if state == "refused" else "")
    app = Starlette()
    app.add_middleware(ModelMaintenanceMiddleware, gate=gate)
    r = TestClient(app).post("/v1/memory.get", json={})
    detail = r.json()["detail"]
    assert (r.status_code, detail["code"]) == (503, code)
    assert (detail["category"], detail["retryable"]) == (category, retryable)


@pytest.mark.parametrize(
    ("ns", "kwargs"),
    [
        ("col-team-docs", {}),  # a collection
        (PERSONAL_NS, {}),  # someone else's personal collection
        ("other", {"assistant": True}),  # the assistant toolset
    ],
)
def test_the_namespace_screen_names_no_step_where_it_would_not_help(
    ns: str, kwargs: dict[str, Any]
) -> None:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.transport import mcp_auth

    auth = AuthInfo(
        subject="viewer@dev",
        org="dev",
        namespaces=("alpha",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    out = mcp_auth._not_accessible_result("loom_memory_get", auth, ns, **kwargs)
    body = out["structuredContent"]
    assert body["code"] == "NAMESPACE_NOT_ACCESSIBLE" and "next" not in body


def test_an_unverified_plain_namespace_refusal_is_answered_as_any_other() -> None:
    """A restricted namespace is refused before the grant read that fails, so a different
    answer for an unverified one would tell it from a namespace that does not exist."""

    from server.transport import mcp_auth

    auth = SimpleNamespace(subject="viewer@dev", org="dev", creator=None)
    checked = mcp_auth._not_accessible_result("loom_memory_get", auth, "other")
    unverified = mcp_auth._not_accessible_result("loom_memory_get", auth, "other", unverified=True)
    assert unverified == checked


def test_a_loom_op_refusal_text_carries_its_class() -> None:
    from server.transport.mcp_op import _result

    out = _result(1, {"error": "no such op", "code": "UNKNOWN_OP"}, error=True)["result"]
    text = json.loads(out["content"][0]["text"])
    assert (text["category"], text["retryable"]) == ("input", False)
    assert text == out["structuredContent"]


def test_the_origin_check_refusal_carries_its_class() -> None:
    from server.main import _refused

    body = json.loads(_refused(403, code="CROSS_SITE_REQUEST", message="m").body)  # type: ignore[attr-defined]
    assert (body["detail"]["category"], body["detail"]["retryable"]) == ("auth", False)


@pytest.mark.parametrize(
    "status", [400, 401, 403, 404, 408, 409, 422, 423, 429, 500, 502, 503, 504]
)
def test_the_sdk_fallback_follows_the_server_rule(status: int) -> None:
    from loom.errors import LoomError

    e = LoomError("m")
    e.status = status
    assert (e.category, e.retryable) == error_class("", status)


def test_mcp_answers_a_revocation_list_not_yet_loaded_with_its_class() -> None:
    from fastapi import Response
    from starlette.requests import Request

    from server.auth.jwt import AuthUnavailable
    from server.transport import mcp_auth

    request = Request({"type": "http", "method": "POST", "path": "/mcp", "headers": []})
    out = mcp_auth._token_refused(request, Response(), {"id": 3}, AuthUnavailable("not loaded"))
    data = out["error"]["data"]
    assert (data["code"], data["category"], data["retryable"]) == (
        "AUTH_UNAVAILABLE",
        "dependency",
        True,
    )


@pytest.mark.parametrize("lasts_s", [None, 90])
def test_a_lock_refusal_is_never_retryable(lasts_s: int | None) -> None:
    """The user lifts a lock; `lock_until` says when one with an end lapses."""
    from datetime import datetime, timedelta

    from server.memory.domain.lock import refusal

    until = None if lasts_s is None else datetime(2030, 1, 1) + timedelta(seconds=lasts_s)
    row = SimpleNamespace(
        memory_key="k", memory_id="m", lock_reason="", locked_by="", lock_until=until
    )
    detail = classified(refusal(row, "write", op="memory.set", mode="read_only").detail, 423)
    assert (detail["category"], detail["retryable"]) == ("input", False)


def test_a_policy_refusal_whose_check_could_not_run_is_retryable() -> None:
    from server.identity.app.policy_gate import PolicyForbiddenError
    from server.identity.domain.policy_rules import PolicyDecision

    unread = PolicyDecision(
        decision="deny",
        determining_rules=[],
        errors=["trace store down"],
        verdicts=[],
        latency_ms=1,
        enforcing_errors=["trace store down"],
    )
    detail = classified(PolicyForbiddenError("m", unread).to_detail(), 403)
    assert (detail["category"], detail["retryable"]) == ("dependency", True)
    denied = PolicyDecision(
        decision="deny",
        determining_rules=["r1"],
        errors=[],
        verdicts=[],
        latency_ms=1,
        enforcing_rules=["r1"],
    )
    detail = classified(PolicyForbiddenError("m", denied).to_detail(), 403)
    assert (detail["category"], detail["retryable"]) == ("auth", False)


def test_an_unverified_role_or_collection_refusal_over_mcp_is_retryable() -> None:
    """Where the answer already differs (a role refusal after the screen admitted the
    namespace, a collection), an unverified check says so in its class."""

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_auth, mcp_call

    auth = SimpleNamespace(subject="viewer@dev", org="dev", creator=None)
    failed = PermissionError("grants could not be read")
    failed.unverified = True  # type: ignore[attr-defined]
    role = mcp_call._forbidden_result(OPERATIONS_BY_NAME["memory.set"], failed, auth, "dev", "a")
    collection = mcp_auth._not_accessible_result(
        "loom_memory_get", auth, "col-team-docs", unverified=True
    )
    for out in (role, collection):
        body = out["structuredContent"]
        assert (body["category"], body["retryable"]) == ("dependency", True)


def test_the_class_in_words_ends_each_text_block_and_leaves_other_blocks_alone() -> None:
    from server.transport.mcp_refusals import classify_tool_errors

    resp = {
        "result": {
            "isError": True,
            "content": [
                {"type": "text", "text": "busy\nexample: {}"},
                {"type": "image", "data": "x"},
            ],
            "structuredContent": {"code": "RATE_LIMITED", "message": "busy"},
        }
    }
    content = classify_tool_errors(resp)["result"]["content"]
    assert content == [
        # On its own line, so the example above it can be copied as is.
        {"type": "text", "text": "busy\nexample: {}\n(capacity: retry later)"},
        {"type": "image", "data": "x"},
    ]


def test_the_class_in_words_names_the_wait_when_the_error_gives_one() -> None:
    from server.transport.mcp_refusals import classify_tool_errors

    body = {"code": "RATE_LIMITED", "message": "busy", "retry_after_s": 30}
    resp = {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "busy"}],
            "structuredContent": body,
        }
    }
    (block,) = classify_tool_errors(resp)["result"]["content"]
    assert block["text"] == "busy\n(capacity: retry after 30 s)"


@pytest.mark.parametrize("code", ["PROJECTION_BEHIND", "ADMISSION_CONTENDED"])
@pytest.mark.parametrize(("idempotent", "retryable"), [(True, True), (False, False)])
def test_handler_failure_detail_keeps_a_503_retryable_only_on_an_idempotent_op(
    code: str, idempotent: bool, retryable: bool
) -> None:
    """A handler's 503 on an op that is not idempotent may follow a write the call made,
    so it is not retryable; on an idempotent op the class stands. The handler marks the
    raw detail first and the exception handler classes it after, keeping the mark."""
    detail = classified(
        handler_failure_detail({"code": code, "message": "m"}, 503, idempotent), 503
    )
    assert detail["retryable"] is retryable
