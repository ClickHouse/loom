"""`POST /v1/admin/bots.provision_agent`: a platform admin makes a trust's agent's
namespace before its first exchange, through the code that exchange runs
(`agents.ensure_namespace`), so its tag, its one owner and the trust's agent
limit hold, and grants can name the agent before it first calls."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import agents, machines, self_signup
from server.auth.jwt import mint_token
from server.identity.app import admin_account_routes
from server.identity.app.directory import IdentityDirectory

_TRUST: machines.Machine = {
    "name": "fleet",
    "org": "acme",
    "ns_roles": [],
    "public_keys": "",
    "issuer": "https://issuer.example",
    "audience": "loom",
    "claims": {"tenant_id": "t1"},
    "agent_claim": "agent_id",
    "jwks_uri": "",
    "max_token_s": 3600,
    "max_agents": 2,
}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The bots, the namespaces the trust made and their owner grants, and the audit log."""
    from server.insights.app import admin_routes
    from server.observability import audit

    state: dict[str, Any] = {
        "bots": {"fleet": _TRUST, "ci": {**_TRUST, "name": "ci", "agent_claim": ""}},
        "rows": {},
        "grants": set(),
        "audits": [],
    }

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> Any:
            if "count()" in sql:  # the trust's live agent namespaces
                live = [r for r in state["rows"].values() if r["active"]]
                return {"n": sum(params["tag"] in r["tags"] for r in live)}
            if "namespace_grants" in sql:
                return {"x": 1} if (params["ns"], params["who"]) in state["grants"] else None
            return state["rows"].get(params["ns"])

    async def get(name: str, *, active_only: bool = True, ch: Any = None) -> Any:
        return state["bots"].get(name)

    async def provision(**kw: Any) -> None:
        state["rows"][kw["namespace"]] = {
            "active": True,
            "tags": kw["tags"],
            "owner_email": kw["subject"],
        }
        state["grants"].add((kw["namespace"], kw["subject"]))

    async def emit(event: str, **kw: Any) -> str:
        state["audits"].append((event, kw))
        return ""

    async def in_use(self: Any, org: str, namespace: str, **_k: Any) -> bool:
        return False  # nothing under the agent's name yet

    monkeypatch.setattr(IdentityDirectory, "in_use", in_use)
    monkeypatch.setattr(agents, "get_client", lambda: _CH())
    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(self_signup, "provision_account", provision)
    monkeypatch.setattr(audit, "emit", emit)
    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: False)
    return state


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_account_routes.router)
    return TestClient(app)


def test_the_namespace_is_made_as_the_first_exchange_makes_it(world) -> None:
    c = _client()
    r = c.post("/v1/admin/bots.provision_agent", json={"name": "fleet", "agent": "a1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"org": "acme", "namespace": "fleet-a1", "subject": "sa:fleet:a1"}
    assert world["rows"]["fleet-a1"] == {
        "active": True,
        "tags": ["machine:fleet"],  # the trust's mark: its agent's exchange adopts it
        "owner_email": "sa:fleet:a1",
    }
    assert ("fleet-a1", "sa:fleet:a1") in world["grants"]
    [(event, audited)] = world["audits"]
    assert event == "auth.agent_provisioned" and audited["actor"] == "admin:local"
    # A repeat changes nothing, and the agent's first exchange finds it made.
    again = c.post("/v1/admin/bots.provision_agent", json={"name": "fleet", "agent": "a1"})
    assert again.json()["namespace"] == "fleet-a1" and len(world["audits"]) == 1
    assert asyncio.run(agents.ensure_namespace(_TRUST, "a1")) == "fleet-a1"
    assert len(world["audits"]) == 1


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ({"name": "nobody", "agent": "a1"}, "no active bot"),
        ({"name": "ci", "agent": "a1"}, "agent claim"),  # one key, one bot: no agents
        ({"name": "fleet", "agent": "A1"}, "lower-case"),  # refused, never rewritten
        ({"name": "fleet", "agent": "a" * 64}, "at most 63"),
    ],
)
def test_an_agent_its_trust_would_not_take_is_refused(world, body, said) -> None:
    r = _client().post("/v1/admin/bots.provision_agent", json=body)
    assert r.status_code == 422 and said in r.text
    assert world["rows"] == {} and world["audits"] == []


def test_the_trusts_tag_owner_and_limit_hold(world) -> None:
    c = _client()
    world["rows"]["fleet-taken"] = {"active": True, "tags": [], "owner_email": "lee"}
    world["rows"]["fleet-gone"] = {"active": False, "tags": ["machine:fleet"], "owner_email": ""}
    for agent, said in (("taken", "not this trust's"), ("gone", "released")):
        r = c.post("/v1/admin/bots.provision_agent", json={"name": "fleet", "agent": agent})
        assert r.status_code == 422 and said in r.text
    for agent in ("a7", "a8"):  # the released one gives its place back
        r = c.post("/v1/admin/bots.provision_agent", json={"name": "fleet", "agent": agent})
        assert r.is_success, r.text
    # a7 and a8 fill `max_agents` (2): the next is refused.
    r = c.post("/v1/admin/bots.provision_agent", json={"name": "fleet", "agent": "a9"})
    assert r.status_code == 422 and "has made its agents" in r.text
    assert "fleet-a9" not in world["rows"]


def test_only_a_platform_admin_provisions(world, monkeypatch) -> None:
    from server.insights.app import admin_routes

    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: True)
    body = {"name": "fleet", "agent": "a1"}
    for roles, status in ((["writer"], 403), (["org_admin"], 403), (["platform_admin"], 200)):
        token = mint_token("ops", "acme", ["*"], roles)
        r = _client().post(
            "/v1/admin/bots.provision_agent",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == status
    assert world["audits"][0][1]["actor"] == "admin:ops"
