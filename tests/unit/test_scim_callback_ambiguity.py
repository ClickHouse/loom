"""Ambiguous callbacks refuse before repairing JIT owner grants."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.auth import aliases, jit, login, oauth, revocation, self_signup, sso_links, tenancy
from server.core import grant_cache, leases
from server.observability import audit
from server.settings import settings
from tests.unit.test_scim_people import people as people

ORG = "acme"
CONTACT = "contact@example.test"
OWNER = "owner@example.test"
STAMP = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)


@dataclass
class _Accounts:
    rows: list[dict[str, Any]] = field(default_factory=list)
    grants: list[dict[str, Any]] = field(default_factory=list)

    def add(self, owner: str, *, org: str = ORG, contact: str = CONTACT) -> None:
        self.rows.append(
            {
                "org": org,
                "namespace": f"sso-account-{len(self.rows)}",
                "owner_email": owner,
                "contact_email": contact,
                "password_hash": "",
                "active": True,
                "tags": [jit.JIT_TAG],
            }
        )


@pytest.fixture
def callback(people, monkeypatch):
    accounts = _Accounts()
    read_many, read_one = people.query_all, people.query_one

    async def query_all(sql, params=None):
        p = params or {}
        if "WHERE contact_email = {ident:String} AND active = 1" in sql:
            return [
                dict(r) for r in accounts.rows if r["contact_email"] == p["ident"] and r["active"]
            ]
        if "toString(owner_email) AS owner" in sql:
            return [
                {
                    "namespace": r["namespace"],
                    "owner": r["owner_email"],
                    "active": r["active"],
                    "tags": r["tags"],
                    "sso": not r["password_hash"],
                }
                for r in accounts.rows
                if r["org"] == p["org"] and r["contact_email"] == p["ident"]
            ]
        if "SELECT owner_email, contact_email" in sql:
            return [
                dict(r)
                for r in accounts.rows
                if r["org"] == p["org"] and p["sub"] in (r["owner_email"], r["contact_email"])
            ]
        if "SELECT org, namespace FROM loom.namespaces FINAL" in sql:
            # These accounts have no explicit SSO links; passwordless rows still qualify.
            return [
                {"org": r["org"], "namespace": r["namespace"]}
                for r in accounts.rows
                if "password_hash = '' OR" in sql
                and r["contact_email"] == p["email"]
                and r["active"]
                and not r["password_hash"]
            ]
        if "password_hash != ''" in sql or "FROM loom.namespace_grants FINAL" in sql:
            return []
        return await read_many(sql, params)

    async def query_one(sql, params=None):
        p = params or {}
        if "SELECT 1 AS x FROM loom.namespace_grants" in sql:
            return next(
                (
                    {"x": 1}
                    for r in accounts.grants
                    if (r["org"], r["namespace"], r["subject"]) == (p["org"], p["ns"], p["sub"])
                ),
                None,
            )
        if "password_hash = '' AS sso" in sql and "namespace = {ns:String}" in sql:
            return next(
                (
                    {**r, "sso": not r["password_hash"]}
                    for r in accounts.rows
                    if (r["org"], r["namespace"]) == (p["org"], p["ns"])
                ),
                None,
            )
        return await read_one(sql, params)

    async def insert(table, rows, **kwargs):
        if table == "loom.namespaces":
            accounts.rows.extend(dict(r) for r in rows)
            people.accounts.extend(
                (r["org"], r["owner_email"], r["contact_email"], not r["password_hash"])
                for r in rows
            )
        else:
            assert table == "loom.namespace_grants"
            accounts.grants.extend(dict(r) for r in rows)

    @asynccontextmanager
    async def holding(*args, **kwargs):
        yield True

    monkeypatch.setattr(people, "query_all", query_all)
    monkeypatch.setattr(people, "query_one", query_one)
    monkeypatch.setattr(people, "insert", insert, raising=False)
    for module in (jit, login, self_signup, sso_links):
        monkeypatch.setattr(module, "get_client", lambda: people)
    monkeypatch.setattr(aliases, "_CACHE", {})
    monkeypatch.setattr(aliases, "_GENERATION", {})
    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(settings, "oidc_jit_org", ORG)
    monkeypatch.setattr(settings, "oidc_jit_email_domains", "")
    monkeypatch.setattr(settings, "platform_admins", "")
    monkeypatch.setattr(self_signup, "naive_utc_now_seconds", lambda: STAMP)
    monkeypatch.setattr(self_signup.time, "time_ns", lambda: 17)
    monkeypatch.setattr(grant_cache, "changed", AsyncMock())
    monkeypatch.setattr(leases, "holding", holding)
    monkeypatch.setattr(revocation, "subjects_revoked", AsyncMock(return_value=False))
    monkeypatch.setattr(tenancy, "ensure_held", AsyncMock())
    monkeypatch.setattr(audit, "emit", AsyncMock())
    return people, accounts


def _owner_grant(org: str, namespace: str, owner: str) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": namespace,
        "subject": owner,
        "role": "admin",
        "granted_at": STAMP,
        "granted_by": owner,
        "revoked_at": None,
        "version": 17,
    }


@pytest.mark.parametrize("ambiguity", ["owners", "orgs", "external-orgs"])
async def test_ambiguous_callback_never_repairs_owner_grants(callback, ambiguity):
    world, accounts = callback
    accounts.add(OWNER, org="other" if ambiguity == "external-orgs" else ORG)
    accounts.add(
        "other@example.test" if ambiguity == "owners" else OWNER,
        org=ORG if ambiguity == "owners" else "third" if ambiguity == "external-orgs" else "other",
    )
    world.accounts = [(r["org"], r["owner_email"], r["contact_email"], True) for r in accounts.rows]
    world.provision(CONTACT)
    assert await login.resolve_identity_for_email(CONTACT) is None
    observed = await oauth._account_for(CONTACT, {"kind": "dashboard"}, "unused-upstream")
    assert accounts.grants == [], (
        f"owner grants written before refusing ambiguity: {accounts.grants}"
    )
    assert isinstance(observed, oauth._Denied)
    assert len(accounts.rows) == 2


@pytest.mark.parametrize("work_alias", [False, True], ids=["primary", "work-alias"])
async def test_unique_callback_repairs_its_qualified_owner(callback, work_alias):
    world, accounts = callback
    contact = CONTACT if work_alias else OWNER
    accounts.add(OWNER, contact=contact)
    world.accounts = [(ORG, OWNER, contact, True)]
    world.provision(OWNER, work=(CONTACT,) if work_alias else ())
    observed = await oauth._account_for(contact, {"kind": "dashboard"}, "unused-upstream")
    assert observed == (OWNER, ORG, ["sso-account-0"], ["writer"])
    assert accounts.grants == [_owner_grant(ORG, "sso-account-0", OWNER)]
    assert len(accounts.rows) == 1


async def test_absent_provisioned_callback_creates_its_jit_account(callback):
    world, accounts = callback
    world.provision(CONTACT)
    assert await login.resolve_identity_for_email(CONTACT) is None
    observed = await oauth._account_for(CONTACT, {"kind": "dashboard"}, "unused-upstream")
    assert observed is not None and not isinstance(observed, oauth._Denied)
    owner, org, namespaces, roles = observed
    assert org == ORG and roles == ["writer"] and namespaces == [owner]
    assert accounts.rows == [
        {
            "org": ORG,
            "namespace": owner,
            "owner_email": owner,
            "contact_email": CONTACT,
            "password_hash": "",
            "description": "Auto-provisioned via OIDC SSO.",
            "tags": [jit.JIT_TAG],
            "active": True,
            "conflict_threshold": None,
            "retention_days": 0,
            "warm_days": 0,
            "visibility": "private",
            "classification": "standard",
            "join_policy": "inherit",
            "created_at": STAMP,
            "updated_at": STAMP,
        }
    ]
    assert accounts.grants == [_owner_grant(ORG, owner, owner)]
