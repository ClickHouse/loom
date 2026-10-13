"""The history policy runs after the read scope, on the input it pins, in both
transports: a token pinned to one user never has a rule evaluated (or a decision
recorded) for another user, which a POLICY_FORBIDDEN answer would leak."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.memory.api.models_crud import MemorySetInput
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import gates, http, mcp_call

PINNED = AuthInfo(
    subject="sa:bot",
    org="o",
    namespaces=("n",),
    roles=(),
    is_service_account=True,
    read_user_id="u1",
)
OP = OPERATIONS_BY_NAME["memory.set"]


@pytest.fixture
def evaluated(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str]]]:
    from server.identity.app import policy_gate

    seen: list[dict[str, str]] = []

    async def gate(op: Any, identity: dict[str, str], raw: Any, *, request_id: str) -> None:
        seen.append(identity)

    monkeypatch.setattr(policy_gate, "gate", gate)
    set_current_auth(PINNED)
    try:
        yield seen
    finally:
        set_current_auth(None)


def _body(user: str) -> MemorySetInput:
    return MemorySetInput(
        org="o", namespace="n", agent="a", user_id=user, memory_key="k", value="v"
    )


def _target() -> gates.Target:
    return gates.authz_target(OP, org="o", namespace="n", auth=PINNED)


def _http(body: MemorySetInput) -> Any:
    call = http._HttpCall(request_id="r", raw_body=body.model_dump(), auth=PINNED)
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    return asyncio.run(http._bind_context_and_policy(OP, request, call, _target(), body))


def _mcp(body: MemorySetInput) -> tuple[Any, Any]:
    call = mcp_call._AdmittedCall(
        1, OP, body, body.model_dump(), PINNED, "r", "", "n", "o", _target()
    )
    return asyncio.run(mcp_call._admission_refusal(call)), call


def test_http_refuses_another_user_before_any_rule_runs(evaluated: list) -> None:
    with pytest.raises(HTTPException) as e:
        _http(_body("u2"))
    assert e.value.status_code == 403
    assert evaluated == []


def test_http_evaluates_rules_for_the_pinned_user(evaluated: list) -> None:
    assert _http(_body("-")).user_id == "u1"
    assert [i["user_id"] for i in evaluated] == ["u1"]


def test_mcp_refuses_another_user_before_any_rule_runs(evaluated: list) -> None:
    refusal, _ = _mcp(_body("u2"))
    assert refusal is not None and refusal["result"]["isError"]
    assert evaluated == []


def test_mcp_evaluates_rules_for_the_pinned_user(evaluated: list, monkeypatch) -> None:
    monkeypatch.setattr(gates, "rate_limit", lambda *a, **k: None)
    refusal, call = _mcp(_body("-"))
    assert refusal is None and call.input_obj.user_id == "u1"
    assert [i["user_id"] for i in evaluated] == ["u1"]


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    from server.observability import ledger

    seen: list[dict[str, str]] = []

    def capture(**kw: Any) -> None:
        seen.append(kw["identity"])

    monkeypatch.setattr(ledger, "emit_agent_event", capture)
    monkeypatch.setattr(http, "emit_agent_event", capture)
    return seen


RAW = {"org": "o", "namespace": "n", "agent": "a", "memory_key": "k", "value": "v"}


async def _http_then_log(body: MemorySetInput, raw: dict[str, Any]) -> None:
    """Admission, then the ledger row, in one context as a request runs them."""
    call = http._HttpCall(request_id="r", raw_body=raw, auth=PINNED)
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    try:
        await http._bind_context_and_policy(OP, request, call, _target(), body)
    except HTTPException:
        pass  # the refusal path logged its own row
    else:
        http._emit_event(OP, "r", raw, 0, success=True, error_code="")


async def _mcp_then_log(body: MemorySetInput, raw: dict[str, Any]) -> None:
    from server.transport import mcp_tools

    call = mcp_call._AdmittedCall(1, OP, body, raw, PINNED, "r", "", "n", "o", _target())
    if await mcp_call._admission_refusal(call) is None:
        mcp_tools._emit_mcp_event(OP.name, raw, 0, True, "", "r")


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_a_pinned_call_is_logged_as_the_user_its_rules_are_keyed_on(
    transport: str, evaluated: list, logged: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule keyed on the user counts the caller's own logged calls: a body
    omitting user_id is logged as the pinned user, as the policy evaluates it."""
    monkeypatch.setattr(gates, "rate_limit", lambda *a, **k: None)
    run = _http_then_log if transport == "http" else _mcp_then_log
    asyncio.run(run(_body("-"), RAW))
    assert [i["user_id"] for i in logged] == ["u1"]
    assert [i["user_id"] for i in evaluated] == ["u1"]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_a_refused_call_naming_another_user_is_logged_as_the_caller(
    transport: str, evaluated: list, logged: list
) -> None:
    """Logged as u2 it would fill u2's history and trip u2's own rules."""
    run = _http_then_log if transport == "http" else _mcp_then_log
    asyncio.run(run(_body("u2"), {**RAW, "user_id": "u2"}))
    assert logged and all(i["user_id"] == "u1" for i in logged)
    assert evaluated == []


def test_each_mcp_call_starts_without_the_previous_calls_scope() -> None:
    """A batch runs its calls in one context: a call refused before its scope is
    resolved must not be logged under the previous call's user and session."""
    from server.identity.domain.read_scope import (
        ReadFilters,
        bind_request_read_filters,
        request_read_filters,
    )
    from server.transport import _common

    token = AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("n",),
        roles=(),
        is_service_account=True,
        read_scope="own_session",
    )

    async def batch() -> dict[str, str]:
        set_current_auth(token)
        try:
            bind_request_read_filters(token, ReadFilters("own_session", "u1", "s1"))
            assert request_read_filters(token).session_id == "s1"
            await mcp_call._bind_caller_identity(OP, {"user_id": "u9"}, token, None)
            return _common.ledger_identity({"user_id": "u9", "session_id": "s9"})
        finally:
            set_current_auth(None)

    logged = asyncio.run(batch())
    assert (logged["user_id"], logged["session_id"]) == ("u9", "s9")
