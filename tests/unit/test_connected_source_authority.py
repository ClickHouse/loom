"""Generic account/source fences shared by publication tests."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import sources
from server.operations.base import Role
from tests.unit.test_mcp_source_sync import harness as harness


class _World:
    def __init__(self, monkeypatch, harness):
        self.source = harness.source
        self.key = (self.source.org, self.source.owner_subject, self.source.connection_slot)
        self.rows = {self.key: harness.account}
        self.fences = {
            accounts._fence_key(harness.account.connection_id): harness.account.connection_id
        }
        self.locks = {}
        self.holders = {}
        self.writes = []
        self.grant = Role.WRITER
        self.account_active = True
        self.scim_ended = False
        self.cutoffs = {}
        monkeypatch.setattr(accounts.leases, "cluster_lease", self.lease)
        monkeypatch.setattr(accounts, "account_lease", self.account_lease)
        monkeypatch.setattr(sources, "sources_lease", self.sources_lease)
        monkeypatch.setattr(accounts.claims, "get", self.claim_get)
        monkeypatch.setattr(accounts.claims, "swap", self.claim_swap)
        monkeypatch.setattr(accounts, "_fence_current", self.fence_current)
        monkeypatch.setattr(accounts.store, "get", self.get)
        monkeypatch.setattr(accounts.store, "put", self.put)
        monkeypatch.setattr(accounts.store, "account_active", self.active)
        monkeypatch.setattr(accounts.source_store, "get_source", self.get_source)
        monkeypatch.setattr(accounts.source_store, "latest_source", self.get_source)
        monkeypatch.setattr(accounts.source_store, "connected_sources", self.connected_sources)
        monkeypatch.setattr(accounts.revocation, "cutoffs", self.revoked)
        monkeypatch.setattr(accounts.scim, "refuses_subject", self.deactivated)
        monkeypatch.setattr(authority.authz, "grant_role_for", self.role)

    @asynccontextmanager
    async def lease(self, key, **kwargs):
        async with self.locks.setdefault(key, asyncio.Lock()):
            task = asyncio.current_task()
            self.holders[task] = self.holders.get(task, 0) + 1
            try:
                yield SimpleNamespace(held=True)
            finally:
                self.holders[task] -= 1

    @asynccontextmanager
    async def account_lease(self, org, owner, slot):
        assert (org, owner, slot) == self.key
        async with self.lease("connected-account:" + slot):
            yield

    @asynccontextmanager
    async def sources_lease(self, org, namespace, **kwargs):
        assert (org, namespace) == ("org", "collection")
        async with self.lease("knowledge-source:org:collection"):
            yield

    async def claim_get(self, ch, key):
        return self.fences.get(key)

    async def claim_swap(self, ch, key, previous, value, expires):
        current = self.fences.get(key)
        if current == previous:
            self.fences[key] = value
            return value
        return current

    async def fence_current(self, account, **kwargs):
        return self.fences.get(accounts._fence_key(account.connection_id)) == account.connection_id

    async def get(self, org, owner, slot, **kwargs):
        return self.rows.get((org, owner, slot))

    async def put(self, row, **kwargs):
        assert self.holders.get(asyncio.current_task(), 0)
        self.rows[row.org, row.subject, row.kind] = row
        self.writes.append(row)

    async def get_source(self, org, namespace, name, **kwargs):
        assert (org, namespace, name) == ("org", "collection", "selected")
        return self.source

    async def connected_sources(self, org, owner, **kwargs):
        assert (org, owner) == ("org", "owner")
        return [self.source] if self.source is not None else []

    async def active(self, *args, **kwargs):
        return self.account_active

    async def deactivated(self, *args, **kwargs):
        return self.scim_ended

    async def revoked(self, spellings):
        assert spellings == ["owner"]
        return self.cutoffs

    async def role(self, *args, **kwargs):
        return self.grant


@pytest.fixture
def world(monkeypatch, harness):
    return _World(monkeypatch, harness)


@pytest.mark.asyncio
async def test_generic_source_requires_its_exact_account_slot_and_generation(world):
    bound = await authority.validate_source(world.source)
    assert bound.source is world.source
    assert bound.connection.account == world.rows[world.key]
    current = world.rows[world.key]
    world.rows[world.key] = replace(
        current, connection_id=accounts._next_connection(current.connection_id)
    )
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_source(world.source)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fence", "owner", "scim", "grant", "cutoff"])
async def test_generic_owner_standing_refuses_without_executing_credentials(world, change):
    await authority.validate_source(world.source)
    if change == "fence":
        world.fences.clear()
    elif change == "owner":
        world.account_active = False
    elif change == "scim":
        world.scim_ended = True
    elif change == "grant":
        world.grant = Role.VIEWER
    else:
        account = world.rows[world.key]
        world.cutoffs = {"owner": int(account.connected_at.timestamp()) + 1}
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_source(world.source)
    assert world.writes == []
