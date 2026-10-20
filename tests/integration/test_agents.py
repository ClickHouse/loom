"""Agents a workload trust tells apart, against a real ClickHouse and Keeper:
two first exchanges at once make one namespace, and a name the trust did not
make is never reached."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from server.auth import agents, machines
from server.core.ch import get_client, own_write_reads
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def ch() -> Any:
    client = get_client()
    await client.connect()
    yield client
    await client.disconnect()


def _trust() -> machines.Machine:
    return {
        "name": f"fleet{uuid.uuid4().hex[:6]}",
        "org": unique_org("agents"),
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


async def test_two_first_exchanges_make_one_namespace(ch: Any) -> None:
    m = _trust()
    made = await asyncio.gather(*(agents.ensure_namespace(m, "a1") for _ in range(3)))
    assert set(made) == {f"{m['name']}-a1"}
    with own_write_reads():
        rows = await ch.query_all(
            "SELECT owner_email, tags FROM loom.namespaces FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            params={"org": m["org"], "ns": made[0]},
        )
    assert [(r["owner_email"], list(r["tags"])) for r in rows] == [
        (f"sa:{m['name']}:a1", [f"machine:{m['name']}"])
    ]


async def test_a_namespace_someone_else_made_is_not_reached(ch: Any) -> None:
    from server.auth.self_signup import provision_account

    m = _trust()
    await provision_account(
        org=m["org"], namespace=f"{m['name']}-a1", subject="lee", contact_email="lee@acme.io"
    )
    with pytest.raises(machines.Refused, match="not this trust's"):
        await agents.ensure_namespace(m, "a1")


async def test_a_released_namespace_is_dropped_from_the_bots_naming_it(ch: Any) -> None:
    org, name = unique_org("agents"), f"bot{uuid.uuid4().hex[:6]}"
    bot: machines.Machine = {
        **_trust(),
        "name": name,
        "org": org,
        "ns_roles": [("team-a", "writer"), ("team-b", "viewer")],
        "agent_claim": "",
    }
    await machines.save(bot, by="it")
    assert await machines.forget_namespace(org, "team-a", "it") == [name]
    kept = await machines.get(name)
    assert kept is not None and kept["ns_roles"] == [("team-b", "viewer")]
    assert kept["issuer"] == bot["issuer"]  # every other column carried forward


async def test_an_agent_is_never_a_sign_in_and_owns_its_namespace(ch: Any) -> None:
    from server.auth.login import resolve_identity_for_email

    m = _trust()
    ns = await agents.ensure_namespace(m, "a1")
    with own_write_reads():
        grants = await ch.query_all(
            "SELECT subject, role FROM loom.namespace_grants FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} AND revoked_at IS NULL ORDER BY subject",
            params={"org": m["org"], "ns": ns},
        )
        nobody = await resolve_identity_for_email("")
    assert [(g["subject"], g["role"]) for g in grants] == [(f"sa:{m['name']}:a1", "admin")]
    assert nobody is None  # its row names no email to sign in with


async def test_two_new_agents_at_once_stay_within_the_trusts_limit(ch: Any) -> None:
    m = {**_trust(), "max_agents": 1}
    outcomes = await asyncio.gather(
        agents.ensure_namespace(m, "a1"),
        agents.ensure_namespace(m, "a2"),
        return_exceptions=True,
    )
    made = [o for o in outcomes if isinstance(o, str)]
    refused = [o for o in outcomes if isinstance(o, machines.Refused)]
    assert len(made) == 1 and len(refused) == 1, outcomes


async def test_two_overlapping_trusts_registered_at_once_save_one(ch: Any) -> None:
    from server.identity.app import account_admin

    issuer, tenant = f"https://issuer-{uuid.uuid4().hex[:6]}.example", uuid.uuid4().hex[:6]
    org = unique_org("agents")

    def trust(name: str) -> machines.Machine:
        return account_admin.bot_from(
            name=name,
            org=org,
            namespace_roles=[],
            issuer=issuer,
            audience="loom",
            claims=[f"tenant_id={tenant}"],
            agent_claim="agent_id",
        )

    a, b = f"tra{uuid.uuid4().hex[:6]}", f"trb{uuid.uuid4().hex[:6]}"
    outcomes = await asyncio.gather(
        account_admin.create_bot(trust(a), by="it", replace=False),
        account_admin.create_bot(trust(b), by="it", replace=False),
        return_exceptions=True,
    )
    refused = [o for o in outcomes if isinstance(o, ValueError)]
    assert len(refused) == 1, outcomes
    saved = [n for n in (a, b) if await machines.get(n) is not None]
    assert len(saved) == 1


async def test_an_agent_reads_what_another_lent_it_until_it_is_taken_back(ch: Any) -> None:
    # One way: a2 reads a1's namespace once a1 shares it, a1 never reads a2's,
    # and a1's unshare ends a2's claim at once, before its token runs out.
    from server.auth.authz import check_authorized
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.api.models_grants import GrantsShareAgentInput, GrantsUnshareAgentInput
    from server.identity.app import agent_shares
    from server.operations.base import Role

    m = _trust()
    a1 = await agents.ensure_namespace(m, "a1")
    a2 = await agents.ensure_namespace(m, "a2")

    def token(agent: str, ns_roles: list[tuple[str, str]]) -> AuthInfo:
        return AuthInfo(
            subject=agents.subject(m["name"], agent),
            org=m["org"],
            namespaces=tuple(ns for ns, _ in ns_roles),
            roles=(),
            is_service_account=True,
            ns_roles=tuple(ns_roles),
        )

    async def roles(agent: str) -> dict[str, str]:
        own = agents.namespace(m["name"], agent)
        with own_write_reads():
            return dict(await agents.with_grants(m, agent, [(own, "writer")]))

    try:
        set_current_auth(token("a1", [(a1, "writer")]))
        await agent_shares.handle_grants_share_agent(
            GrantsShareAgentInput(org=m["org"], namespace=a1, agent="a2")
        )
        assert await roles("a2") == {a2: "writer", a1: "viewer"}
        assert await roles("a1") == {a1: "writer"}

        reader = token("a2", list((await roles("a2")).items()))
        await check_authorized(reader, Role.VIEWER, m["org"], a1)
        with pytest.raises(PermissionError):
            await check_authorized(reader, Role.WRITER, m["org"], a1)

        await agent_shares.handle_grants_unshare_agent(
            GrantsUnshareAgentInput(org=m["org"], namespace=a1, agent="a2")
        )
        with pytest.raises(PermissionError):
            await check_authorized(reader, Role.VIEWER, m["org"], a1)
        assert await roles("a2") == {a2: "writer"}
    finally:
        set_current_auth(None)


async def test_an_agent_neither_lowers_nor_takes_back_an_admins_grant(ch: Any) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.core.errors import GrantWouldDowngradeError, PreconditionFailedError
    from server.identity.api.models_grants import (
        GrantsCreateInput,
        GrantsShareAgentInput,
        GrantsUnshareAgentInput,
    )
    from server.identity.app import agent_shares
    from server.identity.app.grants import handle_grants_create
    from server.identity.store import grants as grants_repo

    m = _trust()
    a1 = await agents.ensure_namespace(m, "a1")
    a2 = agents.subject(m["name"], "a2")
    await handle_grants_create(
        GrantsCreateInput(org=m["org"], namespace=a1, subject=a2, role="writer")
    )
    try:
        set_current_auth(
            AuthInfo(
                subject=agents.subject(m["name"], "a1"),
                org=m["org"],
                namespaces=(a1,),
                roles=(),
                is_service_account=True,
                ns_roles=((a1, "writer"),),
            )
        )
        with pytest.raises(GrantWouldDowngradeError):
            await agent_shares.handle_grants_share_agent(
                GrantsShareAgentInput(org=m["org"], namespace=a1, agent="a2")
            )
        with pytest.raises(PreconditionFailedError):
            await agent_shares.handle_grants_unshare_agent(
                GrantsUnshareAgentInput(org=m["org"], namespace=a1, agent="a2")
            )
    finally:
        set_current_auth(None)
    with own_write_reads():
        row = await grants_repo.find_any_grant(m["org"], a1, a2)
    assert row is not None and row.role == "writer"
