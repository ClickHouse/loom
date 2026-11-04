"""`search.query` over HTTP and `loom_search` over MCP are one operation, so
an admitted call leaves the same trail on both: first-contact provisioning,
the role + namespace gate, the restricted-access audit row, the traffic
class, the history policy gate and the rate limiter.

`loom_search` runs as the registry op, so every step is the shared gate's.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.auth.bearer as bearer_mod
from server.auth.fanout import Fanout
from server.auth.jwt import AuthInfo, TokenError
from server.operations.base import Role
from server.settings import settings

ORG, NS = "o", "n"

_PAYLOAD = {
    "memory": {"results": []},
    "knowledge": {"results": []},
    "profile": {"results": []},
    "conversations": {"results": []},
}


def _principal() -> AuthInfo:
    return AuthInfo(
        subject="alice", org=ORG, namespaces=(NS,), roles=(Role.VIEWER,), is_service_account=False
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Both transports wired to spies on every admission step; the fan-out
    returns empty sections."""
    import server.code.app.member_writes as member_writes
    import server.core.ratelimit as ratelimit_mod
    import server.identity.app.namespace_autoregister as autoreg
    import server.identity.app.policy_gate as policy_gate
    import server.identity.app.restricted_access as restricted
    import server.identity.app.traffic_class_registry as tcr
    import server.transport.mcp_call as mcp_call
    import server.transport.mcp_outcome as mcp_outcome
    from server.memory.app.loom_search import handler as handler_mod
    from server.transport import http as http_mod

    seen: dict[str, list[Any]] = {
        k: [] for k in ("personal", "authz", "restricted", "traffic", "policy", "limit")
    }

    async def _personal(**kw: Any) -> None:
        seen["personal"].append(kw["subject"])

    async def _authorize_op(auth: Any, op: Any, *, org: Any, namespace: Any, **_k: Any) -> None:
        seen["authz"].append((org, namespace))

    async def _check(
        auth: Any, required_role: Any, org: Any = None, namespace: Any = None, **_k: Any
    ) -> None:
        seen["authz"].append((org, namespace))

    async def _restricted(auth: Any, *, op: str, org: Any, namespace: Any, transport: str) -> bool:
        seen["restricted"].append((op, org, namespace))
        return True

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    async def _classify(auth: Any, org: Any, namespace: Any, args: Any) -> tuple[str, str]:
        seen["traffic"].append((org, namespace))
        return "bench", "registry"

    async def _policy(op: Any, identity: dict[str, str], args: Any, *, request_id: str) -> None:
        seen["policy"].append((op.name, identity["org"], identity["namespace"]))

    class _Limiter:
        def check(self, namespace: str, op_name: str, **kw: Any) -> Any:
            seen["limit"].append((namespace, op_name, kw.get("traffic_class")))
            return None  # the gate treats a None answer as "does not apply"

    async def _sections(**_kw: Any) -> dict[str, Any]:
        return {k: dict(v) for k, v in _PAYLOAD.items()}

    async def _fanout(org: str, *_: object) -> Fanout:
        return Fanout(org=org, namespaces=(NS,), cap=32)

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": ORG, "namespace": NS, "agent": "a"}

    monkeypatch.setattr(
        "server.auth.personal_namespace.ensure_personal_namespace_cached", _personal
    )
    monkeypatch.setattr(member_writes, "authorize_op", _authorize_op)
    monkeypatch.setattr("server.auth.authz.check_authorized", _check)
    monkeypatch.setattr(restricted, "note_restricted_access", _restricted)
    monkeypatch.setattr(autoreg, "maybe_autoregister", _noop)
    monkeypatch.setattr(tcr, "classify", _classify)
    monkeypatch.setattr(policy_gate, "gate", _policy)
    monkeypatch.setattr(ratelimit_mod, "get_limiter", lambda: _Limiter())
    monkeypatch.setattr(handler_mod, "search_sections", _sections)
    monkeypatch.setattr("server.auth.fanout.resolve_fanout", _fanout)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    for mod in (mcp_call, mcp_outcome):
        monkeypatch.setattr(mod, "_emit_mcp_event", lambda *a, **k: None)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)

    def _verify(token: str) -> AuthInfo:
        if token != "alice":
            raise TokenError("bad token")
        return _principal()

    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    client = TestClient(app)

    def _http() -> None:
        r = client.post(
            "/v1/search.query",
            json={"org": ORG, "namespace": NS, "query": "coffee"},
            headers={"Authorization": "Bearer alice"},
        )
        assert r.status_code == 200, r.text

    def _mcp() -> None:
        params = {"name": "loom_search", "arguments": {"query": "coffee"}}
        out = asyncio.run(mcp_call._call_tool(1, params, auth=_principal()))
        assert out["result"]["isError"] is False, out

    return {"seen": seen, "http": _http, "mcp": _mcp, "client": client}


def _run(world: dict[str, Any], transport: str) -> dict[str, list[Any]]:
    world[transport]()
    return world["seen"]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_the_role_and_namespace_gate_runs_on_the_addressed_namespace(
    world: dict[str, Any], transport: str
) -> None:
    assert (ORG, NS) in _run(world, transport)["authz"]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_first_contact_provisions_the_personal_namespace(
    world: dict[str, Any], transport: str
) -> None:
    assert _run(world, transport)["personal"] == ["alice"]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_an_admitted_read_reaches_the_restricted_access_audit(
    world: dict[str, Any], transport: str
) -> None:
    assert _run(world, transport)["restricted"] == [("search.query", ORG, NS)]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_the_call_is_classified(world: dict[str, Any], transport: str) -> None:
    assert _run(world, transport)["traffic"] == [(ORG, NS)]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_the_history_policy_gate_sees_the_call(world: dict[str, Any], transport: str) -> None:
    assert _run(world, transport)["policy"] == [("search.query", ORG, NS)]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_the_rate_limiter_draws_the_namespace_bucket_in_the_calls_class(
    world: dict[str, Any], transport: str
) -> None:
    assert _run(world, transport)["limit"] == [(NS, "search.query", "bench")]


@pytest.mark.parametrize("transport", ["http", "mcp"])
def test_an_org_at_its_in_flight_cap_is_refused_and_a_finished_call_frees_its_slot(
    world: dict[str, Any], transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.transport.mcp_call as mcp_call
    from server.core import tenant_slots

    monkeypatch.setattr(settings, "tenant_max_concurrent_requests", 1)
    monkeypatch.setattr(settings, "replica_count", 1)
    monkeypatch.setattr(settings, "web_workers", 1)
    world[transport]()  # under the cap: admitted, and the slot is released after
    assert ORG not in tenant_slots._IN_FLIGHT
    monkeypatch.setitem(tenant_slots._IN_FLIGHT, ORG, 1)  # another call of the org is running
    if transport == "http":
        r = world["client"].post(
            "/v1/search.query",
            json={"org": ORG, "namespace": NS, "query": "coffee"},
            headers={"Authorization": "Bearer alice"},
        )
        assert r.status_code == 429 and r.json()["detail"]["code"] == "RATE_LIMITED"
        assert r.headers["Retry-After"] == "1"
    else:
        params = {"name": "loom_search", "arguments": {"query": "coffee"}}
        out = asyncio.run(mcp_call._call_tool(1, params, auth=_principal()))
        assert out["result"]["isError"] is True
        assert out["result"]["structuredContent"]["code"] == "RATE_LIMITED"
    assert tenant_slots._IN_FLIGHT[ORG] == 1  # the refused call took no slot
