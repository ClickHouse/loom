"""`namespace.register` on a restricted row: only a principal
who passes the two-factor ADMIN check may update it, it can never be made
public, and the registry row keeps `visibility='private'` whatever the form
said."""

from __future__ import annotations

from datetime import datetime

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.identity.api.models import NamespaceRegisterInput
from server.identity.app import namespace as h
from server.operations.base import Role

ORG, PRIV = "clickhouse", "tracker-ops-restricted"


class _Client:
    def __init__(self, existing):
        self.existing = existing
        self.inserted = []

    async def query_one(self, sql, params=None):
        return self.existing

    async def query_all(self, sql, params=None):
        return []

    async def insert(self, table, rows):
        self.inserted.append((table, rows))


@pytest.fixture
def restricted_row(monkeypatch):
    existing = {
        "contact_email": "",
        "password_hash": "",
        "conversation_logging_enabled": False,
        "capture_generation_io": None,
        "coverage_sweep": None,
        "kind": "tracker",
        "owner_email": "ops@x",
        "tags": ["qa"],
        "classification": "restricted",
    }
    client = _Client(existing)
    monkeypatch.setattr(h, "get_client", lambda: client)
    nc.reset()
    nc._cache[ORG] = (1e12, frozenset({PRIV}))
    grants = {}

    async def _grant(org, ns, subject):
        return grants.get((org, ns, subject))

    monkeypatch.setattr(authz, "_grant_role_for", _grant)
    monkeypatch.setattr(h, "resolve_subject", lambda: "ops@x")
    yield client, grants
    nc.reset()
    set_current_auth(None)


def _inp(**kw) -> NamespaceRegisterInput:
    return NamespaceRegisterInput(org=ORG, namespace=PRIV, owner_email="ops@x", **kw)


@pytest.mark.asyncio
async def test_flat_admin_with_wildcard_claim_cannot_update(restricted_row) -> None:
    set_current_auth(
        AuthInfo(
            subject="ops@x",
            org=ORG,
            namespaces=("*",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    with pytest.raises(AuthInsufficientError):
        await h.handle_namespace_register(_inp(description="new"))


@pytest.mark.asyncio
async def test_named_claim_plus_direct_admin_grant_updates_but_stays_private(
    restricted_row,
) -> None:
    client, grants = restricted_row
    grants[(ORG, PRIV, "ops@x")] = Role.ADMIN
    set_current_auth(
        AuthInfo(
            subject="ops@x",
            org=ORG,
            namespaces=(PRIV,),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    out = await h.handle_namespace_register(_inp(description="new", visibility="private"))
    assert out is not None
    ((_t, rows),) = [x for x in client.inserted if x[0] == "loom.namespaces"]
    assert rows[0]["classification"] == "restricted" and rows[0]["visibility"] == "private"


@pytest.mark.asyncio
async def test_public_is_refused_on_a_restricted_row(restricted_row) -> None:
    _client, grants = restricted_row
    grants[(ORG, PRIV, "ops@x")] = Role.ADMIN
    set_current_auth(
        AuthInfo(
            subject="ops@x",
            org=ORG,
            namespaces=(PRIV,),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    with pytest.raises(AuthInsufficientError):
        await h.handle_namespace_register(_inp(visibility="public"))


def test_registry_row_forces_private_for_restricted() -> None:
    now = datetime(2026, 9, 15, 12, 0, 0)
    row = h.registry_row(
        _inp(visibility="public"),
        {"classification": "restricted", "conversation_logging_enabled": False},
        now,
    )
    assert row["visibility"] == "private" and row["classification"] == "restricted"
    row = h.registry_row(_inp(visibility="public"), None, now)
    assert row["visibility"] == "public" and row["classification"] == "standard"
