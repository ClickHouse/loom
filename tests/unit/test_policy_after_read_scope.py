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
