"""An agent's namespace: made on its first exchange as an account is (its row
names its one owner, the agent, and that owner's grant follows), never one
the trust did not make, and finished by a later exchange when cut off."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.auth import agents, machines, self_signup
from server.observability import audit

_NOW = datetime(2026, 10, 16, 12, 0, 0)
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
    "max_agents": 1000,
}


@pytest.fixture
def world(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"rows": {}, "made": [], "granted": [], "grant_rows": set()}

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any]) -> Any:
            if "count()" in sql:
                return {"n": len(state["rows"])}
            if "namespace_grants" in sql:
                return {"x": 1} if (params["ns"], params["who"]) in state["grant_rows"] else None
            return state["rows"].get(params["ns"])

    async def grant(org: str, ns: str, subject: str) -> None:
        state["granted"].append((org, ns, subject))
        state["grant_rows"].add((ns, subject))

    async def provision(**kw: Any) -> None:
        # As provision_account does: the row, then its owner's grant.
        state["made"].append(kw)
        state["rows"][kw["namespace"]] = {
            "active": True,
            "tags": kw["tags"],
            "owner_email": kw["subject"],
        }
        await self_signup.insert_owner_grant(kw["org"], kw["namespace"], kw["subject"])

    async def emit(*a: Any, **k: Any) -> str:
        return ""

    monkeypatch.setattr(agents, "get_client", lambda: _CH())
    monkeypatch.setattr(self_signup, "provision_account", provision)
    monkeypatch.setattr(self_signup, "insert_owner_grant", grant)
    monkeypatch.setattr(audit, "emit", emit)
    return state


def _ensure(trust: machines.Machine = _TRUST, agent: str = "a1") -> str:
    return asyncio.run(agents.ensure_namespace(trust, agent))


def test_the_first_exchange_makes_the_agents_namespace_once(world) -> None:
    assert _ensure() == _ensure() == "fleet-a1"
    [made] = world["made"]
    assert made["subject"] == "sa:fleet:a1" and made["tags"] == ["machine:fleet"]
    assert made["contact_email"] == ""  # a sign-in by email never resolves to an agent
    assert world["granted"] == [("acme", "fleet-a1", "sa:fleet:a1")]  # its one owner


def test_a_name_the_trust_did_not_make_is_refused(world) -> None:
    for row in (
        {"active": True, "tags": [], "owner_email": "lee"},  # someone's own namespace
        {"active": True, "tags": ["machine:fleet"], "owner_email": "mallory"},  # a forged tag
    ):
        world["rows"]["fleet-a1"] = row
        with pytest.raises(machines.Refused, match="not this trust's"):
            _ensure()
    assert world["made"] == []


def test_a_released_agent_namespace_is_not_made_again(world) -> None:
    world["rows"]["fleet-a1"] = {
        "active": False,
        "tags": ["machine:fleet"],
        "owner_email": "sa:fleet:a1",
    }
    with pytest.raises(machines.Refused, match="released"):
        _ensure()


@pytest.mark.parametrize(("value", "ok"), [("a1", True), ("agent-7", True), ("A1", False)])
def test_the_agent_claim_is_taken_as_given_or_refused(value: str, ok: bool) -> None:
    if ok:
        assert agents.agent_of(_TRUST, {"agent_id": value}) == value
    else:
        with pytest.raises(machines.Refused):
            agents.agent_of(_TRUST, {"agent_id": value})


def test_a_caller_cannot_give_a_namespace_a_trusts_mark() -> None:
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    inp = NamespaceRegisterInput(
        org="acme", namespace="fleet-victim", owner_email="mallory", tags=["machine:fleet", "x"]
    )
    assert registry_row(inp, None, _NOW)["tags"] == ["x"]
    stored = {
        **registry_row(inp, None, _NOW),
        "tags": ["machine:fleet"],
        "owner_email": "sa:fleet:victim",
    }
    kept = registry_row(inp, stored, _NOW)
    assert kept["tags"] == ["x", "machine:fleet"]  # the trust's own mark stays
    assert kept["owner_email"] == "sa:fleet:victim"  # and its binding to the agent


def test_a_trust_makes_no_more_agents_than_its_limit(world) -> None:
    trust = {**_TRUST, "max_agents": 1}
    assert _ensure(trust) == _ensure(trust) == "fleet-a1"  # still its own
    with pytest.raises(machines.Refused, match="made its agents"):
        _ensure(trust, "a2")


def test_a_first_exchange_cut_off_before_its_grant_is_finished_by_the_next(
    world, monkeypatch
) -> None:
    real = self_signup.insert_owner_grant
    calls = {"n": 0}

    async def fails_once(org: str, ns: str, subject: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ClickHouse away")
        await real(org, ns, subject)

    monkeypatch.setattr(self_signup, "insert_owner_grant", fails_once)
    with pytest.raises(RuntimeError):
        _ensure()  # the row landed, its grant did not
    _ensure()  # the next exchange finishes it
    assert world["granted"] == [("acme", "fleet-a1", "sa:fleet:a1")]


def test_a_grant_revoked_since_stays_revoked(world) -> None:
    _ensure()
    world["granted"].clear()  # its row, revoked or not, stays in grant_rows
    _ensure()
    assert world["granted"] == []


def test_the_repair_holds_the_grants_claim(world, monkeypatch) -> None:
    # A revocation of that grant runs under its claim: the repair's read and
    # write must not straddle one.
    from contextlib import asynccontextmanager

    from server.auth import grant_claims

    held: list[tuple[str, str, str]] = []

    @asynccontextmanager
    async def claimed(org: str, ns: str, subject: str) -> Any:
        held.append((org, ns, subject))
        yield

    monkeypatch.setattr(grant_claims, "grant_change", claimed)
    world["rows"]["fleet-a1"] = {
        "active": True,
        "tags": ["machine:fleet"],
        "owner_email": "sa:fleet:a1",
    }
    _ensure()
    assert held == [("acme", "fleet-a1", "sa:fleet:a1")]
    assert world["granted"] == [("acme", "fleet-a1", "sa:fleet:a1")]
