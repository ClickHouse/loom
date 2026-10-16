"""An agent's namespace: made on its first exchange, never one the trust did
not make, and its owner's when the trust vouches for one with an account."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import agents, login, machines, self_signup
from server.observability import audit

_TRUST: machines.Machine = {
    "name": "fleet",
    "org": "acme",
    "ns_roles": [],
    "public_keys": "",
    "issuer": "https://issuer.example",
    "audience": "loom",
    "claims": {"aud": "loom"},
    "agent_claim": "agent_id",
    "owner_claim": "owner",
    "jwks_uri": "",
}


@pytest.fixture
def world(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"rows": {}, "made": [], "granted": [], "people": {}}

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> Any:
            return state["rows"].get(params["ns"])

    async def provision(**kw: Any) -> None:
        state["made"].append(kw)
        state["rows"][kw["namespace"]] = {"active": True, "tags": kw["tags"]}

    async def grant(org: str, ns: str, subject: str) -> None:
        state["granted"].append((org, ns, subject))

    async def person(email: str) -> Any:
        return state["people"].get(email)

    async def emit(*a: Any, **k: Any) -> str:
        return ""

    monkeypatch.setattr(agents, "get_client", lambda: _CH())
    monkeypatch.setattr(self_signup, "provision_account", provision)
    monkeypatch.setattr(self_signup, "insert_owner_grant", grant)
    monkeypatch.setattr(login, "resolve_identity_for_email", person)
    monkeypatch.setattr(audit, "emit", emit)
    return state


def _ensure(owner: str = "") -> str:
    return asyncio.run(agents.ensure_namespace(_TRUST, "a1", owner))


def test_the_first_exchange_makes_the_agents_namespace_once(world) -> None:
    assert _ensure() == _ensure() == "fleet-a1"
    [made] = world["made"]
    assert made["subject"] == "sa:fleet:a1" and made["tags"] == ["machine:fleet"]


def test_a_name_the_trust_did_not_make_is_refused(world) -> None:
    world["rows"]["fleet-a1"] = {"active": True, "tags": []}  # someone's own namespace
    with pytest.raises(machines.Refused, match="not this trust's"):
        _ensure()
    assert world["made"] == []


def test_a_released_agent_namespace_is_not_made_again(world) -> None:
    world["rows"]["fleet-a1"] = {"active": False, "tags": ["machine:fleet"]}
    with pytest.raises(machines.Refused, match="released"):
        _ensure()


def test_the_owner_gets_admin_only_in_the_trusts_org(world) -> None:
    world["people"] = {
        "lee@acme.io": ("lee", "acme", ["lee"], ["writer"]),
        "kim@other.io": ("kim", "other", ["kim"], ["writer"]),
    }
    _ensure("lee@acme.io")
    assert world["granted"] == [("acme", "fleet-a1", "lee")]
    world["rows"].clear()
    _ensure("kim@other.io")
    assert world["granted"] == [("acme", "fleet-a1", "lee")]  # nothing outside acme


@pytest.mark.parametrize(("value", "ok"), [("a1", True), ("agent-7", True), ("A1", False)])
def test_the_agent_claim_is_taken_as_given_or_refused(value: str, ok: bool) -> None:
    if ok:
        assert agents.agent_of(_TRUST, {"agent_id": value}) == value
    else:
        with pytest.raises(machines.Refused):
            agents.agent_of(_TRUST, {"agent_id": value})
