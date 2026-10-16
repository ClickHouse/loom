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
        "org": f"agents-it-{uuid.uuid4().hex[:6]}",
        "ns_roles": [],
        "public_keys": "",
        "issuer": "https://issuer.example",
        "audience": "loom",
        "claims": {"aud": "loom"},
        "agent_claim": "agent_id",
        "owner_claim": "",
        "jwks_uri": "",
    }


async def test_two_first_exchanges_make_one_namespace(ch: Any) -> None:
    m = _trust()
    made = await asyncio.gather(*(agents.ensure_namespace(m, "a1", "") for _ in range(3)))
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
        await agents.ensure_namespace(m, "a1", "")


async def test_a_released_namespace_is_dropped_from_the_bots_naming_it(ch: Any) -> None:
    org, name = f"agents-it-{uuid.uuid4().hex[:6]}", f"bot{uuid.uuid4().hex[:6]}"
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


async def test_an_agent_is_never_a_sign_in_and_its_owner_gets_admin(ch: Any) -> None:
    from server.auth.login import resolve_identity_for_email
    from server.auth.self_signup import provision_account

    m = {**_trust(), "owner_claim": "owner"}
    alice, nobody = f"alice-{uuid.uuid4().hex[:6]}@x.io", f"bob-{uuid.uuid4().hex[:6]}@x.io"
    await provision_account(
        org=m["org"], namespace=f"alice{uuid.uuid4().hex[:6]}", subject="alice", contact_email=alice
    )
    ns = await agents.ensure_namespace(m, "a1", alice)
    other = await agents.ensure_namespace(m, "a2", nobody)
    with own_write_reads():
        person = await resolve_identity_for_email(alice)
        stranger = await resolve_identity_for_email(nobody)
        grants = await ch.query_all(
            "SELECT subject, role FROM loom.namespace_grants FINAL WHERE org = {org:String} "
            "AND namespace = {ns:String} AND revoked_at IS NULL ORDER BY subject",
            params={"org": m["org"], "ns": ns},
        )
    assert person is not None and person[0] == "alice"  # still signs in to their own
    assert stranger is None  # an email the trust named is not the agent's sign-in
    assert [(g["subject"], g["role"]) for g in grants] == [
        ("alice", "admin"),
        (f"sa:{m['name']}:a1", "admin"),
    ]
    assert other == f"{m['name']}-a2"
