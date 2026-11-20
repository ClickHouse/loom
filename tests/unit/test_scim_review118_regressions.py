"""Provisioning distinguishes resources and resolves verified work aliases read-only."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.auth import aliases, jit, login, oauth
from server.cli.cell_ch import Refused
from server.settings import settings
from tests.unit.test_cell_scim_precondition import provisioned_run as provisioned_run
from tests.unit.test_scim_people import people as people

OWNER = "owner@example.test"
CONTACT = "contact@example.test"


@pytest.mark.parametrize("revoke", [False, True], ids=["check", "explicit-revoke"])
async def test_ownerless_namespace_is_not_a_person_account(provisioned_run, revoke):
    run, world, _native = provisioned_run
    run.args.revoke_unprovisioned = revoke
    world.accounts = [("o", "", "", True), ("o", OWNER, OWNER, True)]
    world.provision(OWNER)
    try:
        observed = await run.check_beats("refuse")
    except Refused as error:
        observed = error
    assert not isinstance(observed, Refused), str(observed)
    assert observed.scim is True
    assert run.report["scim"]["checked"] == 1
    assert run.report["scim"]["unprovisioned"] == 0
    assert world.writes == []


async def test_blank_personal_creator_still_refuses(provisioned_run):
    run, world, _native = provisioned_run
    world.services_census = [("o", "orphan", "o", "sa:orphan", "", "", 0, 1)]
    world.services["sa:orphan"] = {"created_by": "", "lineage": []}
    with pytest.raises(Refused) as refused:
        await run.check_beats("refuse")
    assert "unprovisioned services subject 'sa:orphan'" in str(refused.value)
    assert world.writes == []


async def test_whitespace_owner_is_not_an_ownerless_namespace(provisioned_run):
    run, world, _native = provisioned_run
    world.accounts = [("o", " ", "", True)]
    with pytest.raises(Refused):
        await run.check_beats("refuse")
    assert run.report["scim"]["unprovisioned"] == 1
    assert world.writes == []


@pytest.fixture
def callback(people, monkeypatch):
    people.accounts = [("acme", OWNER, CONTACT, True)]
    reads = people.query_all
    lookups = []

    async def query_all(sql, params=None):
        p = params or {}
        if "WHERE contact_email = {ident:String} AND active = 1" in sql:
            lookups.append(p["ident"])
            return [
                {
                    "namespace": f"sso-account-{index}",
                    "owner_email": owner,
                    "org": org,
                    "password_hash": "",
                    "contact_email": email,
                }
                for index, (org, owner, email, verified) in enumerate(people.accounts)
                if email == p["ident"] and verified
            ]
        if "SELECT owner_email, contact_email" in sql:
            return [
                {"owner_email": owner, "contact_email": email}
                for org, owner, email, verified in people.accounts
                if org == p["org"] and verified and p["sub"] in (owner, email)
            ]
        if "loom.namespace_grants" in sql:
            return []
        return await reads(sql, params)

    monkeypatch.setattr(people, "query_all", query_all)
    monkeypatch.setattr(login, "get_client", lambda: people)
    monkeypatch.setattr(aliases, "_CACHE", {})
    monkeypatch.setattr(aliases, "_GENERATION", {})
    monkeypatch.setattr(settings, "platform_admins", "")
    finish = AsyncMock(return_value=None)
    make = AsyncMock(side_effect=AssertionError("the callback must not create an account"))
    monkeypatch.setattr(jit, "finish_unfinished_account", finish)
    monkeypatch.setattr(jit, "jit_provision_for_email", make)
    return people, finish, make, lookups


async def test_verified_work_alias_uses_actual_canonical_owner_policy(callback):
    world, finish, make, lookups = callback
    world.provision(OWNER, work=(CONTACT,))
    observed = await oauth._account_for(CONTACT, {"kind": "dashboard"}, "unused-upstream")
    assert observed == (OWNER, "acme", ["sso-account-0"], ["writer"])
    assert lookups
    assert finish.await_count == 1
    assert make.await_count == 0


@pytest.mark.parametrize("status", ["unknown", "inactive"])
async def test_unqualified_callback_never_finishes_or_jits(callback, status):
    world, finish, make, _lookups = callback
    world.provision(OWNER, work=(CONTACT,))
    email = "unknown@example.test" if status == "unknown" else CONTACT
    if status == "inactive":
        world.provision(CONTACT, active=False)
    finish.side_effect = AssertionError("policy must precede unfinished-account writes")
    observed = await oauth._account_for(email, {"kind": "dashboard"}, "unused-upstream")
    assert isinstance(observed, oauth._Denied)
    assert finish.await_count == 0
    assert make.await_count == 0


async def test_active_primary_contact_preserves_existing_callback(callback):
    world, finish, make, _lookups = callback
    world.provision(OWNER)
    world.provision(CONTACT)
    assert await oauth._account_for(CONTACT, {"kind": "dashboard"}, "unused-upstream") == (
        OWNER,
        "acme",
        ["sso-account-0"],
        ["writer"],
    )
    assert finish.await_count == 1
    assert make.await_count == 0
