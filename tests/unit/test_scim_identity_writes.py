"""Identity writes invalidate complete decisions before and after partial writes."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from server.auth import machines, scim, self_signup, sso_links
from server.auth.delegation import Delegated
from server.core import grant_cache, versions
from server.identity.app import me_service_accounts
from server.settings import settings
from tests.unit.test_scim_people import people as people

EMAIL = "alice@example.test"


@asynccontextmanager
async def announced(*args, **kwargs):
    yield


@pytest.mark.parametrize("outcome", ["success", "lost-reply", "cancel"])
@pytest.mark.parametrize("kind", ["account", "service-account", "sso-link", "machine", "scim"])
async def test_real_identity_writers_discard_decisions_before_and_after_the_write(
    people, monkeypatch, kind, outcome
):
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 240)
    people.provision(EMAIL)
    people.accounts.append(("acme", "alice", EMAIL, kind != "sso-link"))

    async def next_version(*args, **kwargs):
        return 2

    async def no_side_effect(*args, **kwargs):
        return None

    monkeypatch.setattr(versions, "next_version", next_version)
    monkeypatch.setattr(machines, "next_version", next_version)
    monkeypatch.setattr(scim, "next_version", next_version)
    monkeypatch.setattr(sso_links, "next_version", next_version)
    monkeypatch.setattr(scim, "_audit_transition", no_side_effect)
    monkeypatch.setattr(self_signup, "insert_owner_grant", no_side_effect)
    monkeypatch.setattr(me_service_accounts, "hand_down_lineage", no_side_effect)
    monkeypatch.setattr(grant_cache, "announced", announced)
    subject = {
        "account": "alice",
        "service-account": "sa:personal:one",
        "sso-link": "alice",
        "machine": "sa:robot:agent",
        "scim": EMAIL,
    }[kind]
    before = kind in ("service-account", "sso-link")
    if kind == "machine":
        people.bots["robot"] = True
    assert await scim.refuses_subject(subject, org="acme") is before
    warmed_reads = len(people.reads)
    expected_error = (
        ConnectionError("identity row landed but the reply was lost")
        if outcome == "lost-reply"
        else asyncio.CancelledError()
    )
    writes = []

    async def insert(table, rows, **kwargs):
        assert await scim.refuses_subject(subject, org="acme") is before
        assert len(people.reads) > warmed_reads, "a write must start with a fresh complete decision"
        writes.extend((table, dict(row)) for row in rows)
        for row in rows:
            if table == "loom.namespaces":
                people.accounts.append(
                    (row["org"], row["owner_email"], row["contact_email"], not row["password_hash"])
                )
            elif table == "loom.service_accounts":
                people.services[row["sa_subject"]] = dict(row)
            elif table == "loom.sso_links":
                people.accounts[:] = [
                    (org, owner, email, bool(row["linked"]))
                    for org, owner, email, _ in people.accounts
                ]
            elif table == "loom.machine_identities":
                people.bots[row["name"]] = bool(row["active"])
            elif table == "loom.scim_users":
                people.users[row["id"]] = dict(row)
            else:
                pytest.fail(f"unexpected identity write: {table}")
        if outcome != "success":
            raise expected_error

    monkeypatch.setattr(people, "insert", insert, raising=False)

    async def write():
        if kind == "account":
            await self_signup.provision_account(
                org="acme",
                namespace="new-account",
                subject="alice",
                contact_email="missing@example.test",
                password_hash="unit-password-hash",
                ch=people,
            )
        elif kind == "service-account":
            delegated = Delegated(
                org="acme",
                namespaces=["private"],
                roles=["writer"],
                ns_roles=None,
                write_namespaces=None,
                read_scope="",
                read_user_id="",
                ttl_seconds=60,
                parents=[],
                parent_subjects=[],
                parent_iats=[],
                creator="alice",
                is_service_account=True,
            )
            await me_service_accounts.save_token(
                "personal", subject, delegated, created_by="alice", now=datetime.now(UTC)
            )
        elif kind == "sso-link":
            await sso_links._write(EMAIL, [("acme", "private")], "alice", linked=1, ch=people)
        elif kind == "machine":
            await machines.save(
                {
                    "name": "robot",
                    "org": "acme",
                    "ns_roles": [],
                    "public_keys": "",
                    "issuer": "",
                    "audience": "",
                    "claims": {},
                    "agent_claim": "",
                    "jwks_uri": "",
                    "max_token_s": 60,
                    "max_agents": 1,
                    "max_access_token_s": 60,
                    "platform_admin": False,
                },
                by="alice",
                active=False,
                ch=people,
            )
        else:
            row = {
                "id": scim.user_id(EMAIL),
                "email": EMAIL,
                "user_name": EMAIL,
                "emails": json.dumps([{"value": EMAIL, "type": "work", "primary": True}]),
                "addresses": [EMAIL],
                "work_addresses": [EMAIL],
                "active": True,
                "deleted": False,
                "subjects": [EMAIL],
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
                "version": 1,
            }
            await scim._write_audited(row, {**row, "active": False})

    if outcome == "success":
        await write()
    else:
        with pytest.raises(type(expected_error)) as caught:
            await write()
        assert caught.value is expected_error
    assert len(writes) == 1
    assert await scim.refuses_subject(subject, org="acme") is not before
    assert len(people.reads) > warmed_reads
