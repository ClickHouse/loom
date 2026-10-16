"""The operator's bot actions: a bot is one org's, on namespaces that exist,
and replacing or disabling it ends the tokens it holds."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import machines, revocation
from server.identity.app import account_admin
from server.identity.store import namespaces

_KEY = machines.generate_key()[1].decode()


@pytest.mark.parametrize(
    ("org", "roles"),
    [
        ("*", ["shared=writer"]),
        ("", ["shared=writer"]),
        ("acme", ["*=writer"]),
        ("acme", ["a b=viewer"]),
    ],
)
def test_a_bot_is_one_orgs_on_named_namespaces(org: str, roles: list[str]) -> None:
    with pytest.raises(ValueError):
        account_admin.bot_from(name="b", org=org, namespace_roles=roles, public_key_pem=_KEY)


@pytest.mark.parametrize("ns", ["acme.prod", "Team_A", "u-lee-1a2b3c4d"])
def test_any_registered_namespace_name_will_do(ns: str) -> None:
    bot = account_admin.bot_from(
        name="b", org="acme", namespace_roles=[f"{ns}=writer"], public_key_pem=_KEY
    )
    assert bot["ns_roles"] == [(ns, "writer")]


def test_a_trust_issuer_is_kept_as_given_slash_and_all() -> None:
    # Some issuers' `iss` ends in a slash, and a token's `iss` is compared exactly.
    bot = account_admin.bot_from(
        name="deploy",
        org="acme",
        namespace_roles=["deploys=writer"],
        issuer="https://sts.example/tenant/",
        audience="loom",
        claims=["repository=acme/app"],
    )
    assert bot["issuer"] == "https://sts.example/tenant/"


@pytest.fixture
def world(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"bots": {}, "revoked": [], "outright": set(), "namespaces": {"triage"}}

    async def get(name: str) -> machines.Machine | None:
        return state["bots"].get(name)

    async def save(m: machines.Machine, *, by: str, active: bool = True) -> None:
        if active:
            state["bots"][m["name"]] = m
        else:
            state["bots"].pop(m["name"], None)

    async def is_active(org: str, namespace: str, **kw: Any) -> bool:
        return namespace in state["namespaces"]

    async def revoked_outright(jti: str) -> bool:
        return jti in state["outright"]

    async def revoke(token_id: str, **kw: Any) -> dict[str, Any]:
        state["revoked"].append((token_id, kw["reason"]))
        return {}

    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(machines, "save", save)
    monkeypatch.setattr(namespaces, "is_active", is_active)
    monkeypatch.setattr(revocation, "revoked_outright", revoked_outright)
    monkeypatch.setattr(revocation, "revoke", revoke)
    monkeypatch.setattr(account_admin.audit, "emit", emit)
    return state


def _bot(roles: list[str]) -> machines.Machine:
    return account_admin.bot_from(
        name="triage", org="acme", namespace_roles=roles, public_key_pem=_KEY
    )


def test_a_bot_on_a_namespace_that_does_not_exist_is_refused(world) -> None:
    with pytest.raises(ValueError, match="no namespace ghost"):
        asyncio.run(account_admin.create_bot(_bot(["ghost=writer"]), by="ops", replace=False))


def test_a_subject_revoked_outright_makes_no_bot(world) -> None:
    world["outright"].add("sa:triage")
    with pytest.raises(ValueError, match="revoked outright"):
        asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))


def test_replacing_a_bot_ends_the_tokens_it_had(world) -> None:
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    assert world["revoked"] == []
    with pytest.raises(ValueError, match="exists"):
        asyncio.run(account_admin.create_bot(_bot(["triage=viewer"]), by="ops", replace=False))
    asyncio.run(account_admin.create_bot(_bot(["triage=viewer"]), by="ops", replace=True))
    assert world["revoked"] == [("sa:triage", "bot replaced")]
    assert asyncio.run(account_admin.disable_bot("triage", by="ops"))
    assert world["revoked"][-1] == ("sa:triage", "bot disabled")
