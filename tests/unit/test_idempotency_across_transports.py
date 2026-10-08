"""One idempotency key replays across transports.

Hashing the raw HTTP body and MCP's `{"tool", "arguments"}` separately would
give a client that retries over the other transport with the same key a 409
conflict instead of its stored answer. Both hash the op's validated input
(`idempotency.request_hash`); the key is already scoped to (org, subject, op).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

import server.auth.bearer as bearer_mod
import server.core.idempotency as idempotency
import server.transport.mcp_call as mcp_call
import server.transport.mcp_outcome as mcp_outcome
from server.auth.jwt import AuthInfo, TokenError
from server.operations.base import Operation, Role
from server.settings import settings

TOOL = "loom_test_idem_across"


class _In(BaseModel):
    org: str
    namespace: str
    agent: str = "-"
    text: str = ""
    run_id: str = "-"


class _Out(BaseModel):
    calls: int = 0


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import server.code.app.member_writes as member_writes
    from server.transport import http as http_mod

    ran: list[str] = []
    rows: dict[str, tuple[str, Any]] = {}

    async def _handler(inp: Any) -> _Out:
        ran.append(inp.text)
        return _Out(calls=len(ran))

    op = Operation(
        name="test.idem_across",
        input_model=_In,
        output_model=_Out,
        handler=_handler,
        idempotent=True,
        budget_p99_ms=10_000,
        required_role=Role.VIEWER,
    )

    async def _check(key: str, body_hash: str) -> idempotency.CacheResult:
        row = rows.get(key)
        if row is None:
            return idempotency.CacheResult(hit=None, conflict=False)
        if row[0] != body_hash:
            return idempotency.CacheResult(hit=None, conflict=True)
        return idempotency.CacheResult(
            hit=idempotency.CacheHit(status_code=200, body=row[1], headers={}), conflict=False
        )

    owners: list[tuple[str, str, str]] = []

    async def _store(idempotency_key: str, body_hash: str, **kw: Any) -> None:
        rows[idempotency_key] = (body_hash, kw["response_body"])
        owners.append((idempotency_key, kw["org"], kw["subject"]))

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    alice = AuthInfo(
        subject="alice", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=True
    )

    def _verify(token: str) -> AuthInfo:
        if token != "alice":
            raise TokenError("bad token")
        return alice

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr(idempotency, "check", _check)
    monkeypatch.setattr(idempotency, "store", _store)
    monkeypatch.setattr(member_writes, "authorize_op", _noop)
    monkeypatch.setattr("server.transport.gates._admitted", _noop)
    monkeypatch.setattr("server.auth.personal_namespace.ensure_personal_namespace_cached", _noop)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setitem(mcp_call._TOOLS_BY_NAME, TOOL, op)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    return {"http": TestClient(app), "ran": ran, "alice": alice, "owners": owners}


BODY = {"org": "o", "namespace": "n", "agent": "a", "text": "hi"}


def _http(env: dict[str, Any], body: dict[str, Any], key: str) -> Any:
    return env["http"].post(
        "/v1/test.idem_across",
        json=body,
        headers={"Authorization": "Bearer alice", "Idempotency-Key": key},
    )


def _mcp(env: dict[str, Any], body: dict[str, Any], key: str) -> dict[str, Any]:
    params = {"name": TOOL, "arguments": {**body, "_idempotency_key": key}}
    return asyncio.run(mcp_call._call_tool(1, params, auth=env["alice"]))["result"]


def test_an_http_key_replays_over_mcp(env: dict[str, Any]) -> None:
    assert _http(env, BODY, "k1").status_code == 200
    out = _mcp(env, BODY, "k1")
    assert out["isError"] is False and out.get("_meta", {}).get("idempotentReplay") is True
    assert env["ran"] == ["hi"]


def test_an_mcp_key_replays_over_http(env: dict[str, Any]) -> None:
    assert _mcp(env, BODY, "k2")["isError"] is False
    r = _http(env, BODY, "k2")
    assert r.status_code == 200 and r.headers.get("X-Idempotent-Replay") == "true"
    assert env["ran"] == ["hi"]


def test_a_different_body_is_still_a_conflict_across_transports(env: dict[str, Any]) -> None:
    assert _http(env, BODY, "k3").status_code == 200
    out = _mcp(env, {**BODY, "text": "other"}, "k3")
    assert out["isError"] is True and out["structuredContent"]["code"] == "IDEMPOTENT_CONFLICT"


def test_the_hash_is_the_validated_input_and_ignores_a_stamped_run() -> None:
    a = _In(org="o", namespace="n", text="hi")
    b = _In(org="o", namespace="n", text="hi", run_id="run-session-9")
    assert idempotency.request_hash(a) == idempotency.request_hash(b, unset=("run_id",))
    assert idempotency.request_hash(a) != idempotency.request_hash(b)


def test_both_transports_stamp_the_same_key_and_owner(env: dict[str, Any]) -> None:
    """The row erasure deletes by org and subject is the one a replay reads:
    one scoped key per request whichever transport stored it."""
    assert _http(env, BODY, "k4").status_code == 200
    assert _mcp(env, {**BODY, "text": "mcp"}, "k5")["isError"] is False
    want = [
        (
            idempotency.scoped_key(k, org="o", subject="alice", operation="test.idem_across"),
            "o",
            "alice",
        )
        for k in ("k4", "k5")
    ]
    assert env["owners"] == want
