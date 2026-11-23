"""Local retirement fences credentials and source work without provider HTTP."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from server.erasure.store import census
from server.knowledge.app import connected_accounts as connections
from server.knowledge.app import source_push, sources
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.sources import KnowledgeSource


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, kinds: tuple[str, ...] = ("gdrive",)):
        self.accounts: dict[tuple[str, str, str], ConnectedAccount] = {}
        self.sources: dict[str, KnowledgeSource] = {}
        self.fences: dict[str, str] = {}
        self.events: list[tuple[str, str]] = []
        self.depth = 0
        self.fail_next_account_write = False
        self.stale_sources: list[KnowledgeSource] | None = None
        for i, kind in enumerate(kinds):
            account = ConnectedAccount(
                "team",
                "person",
                kind,
                str(i + 1) * 32 + "/1" + "a" * 31,
                credential_enc="sealed-refresh",
                active=True,
                connected_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            self.accounts["team", "person", kind] = account
            self.fences[connections._fence_key(account.connection_id)] = account.connection_id
            self.sources[kind] = KnowledgeSource(
                "team",
                "shared-space",
                kind,
                kind,
                "source-" + kind,
                credential_mode="connected",
                owner_subject="person",
                connection_id=account.connection_id,
                access="mirrored",
            )
        monkeypatch.setattr(connections, "account_lease", self.lease)
        monkeypatch.setattr(sources, "sources_lease", self.lease)
        monkeypatch.setattr(connections, "get_client", object)
        monkeypatch.setattr(connections.claims, "swap", self.swap)
        monkeypatch.setattr(connections.claims, "get", self.fence)
        monkeypatch.setattr(connections.store, "get", self.get)
        monkeypatch.setattr(connections.store, "put", self.put)
        monkeypatch.setattr(connections.store, "owned", self.owned)
        monkeypatch.setattr(connections.store, "erase", self.erase)
        monkeypatch.setattr(census, "_default_client", lambda: self)
        monkeypatch.setattr(connections.source_store, "connected_sources", self.connected_sources)
        monkeypatch.setattr(connections.source_store, "latest_source", self.latest_source)
        monkeypatch.setattr(connections.source_store, "insert_source", self.insert_source)
        monkeypatch.setattr(sources, "_queue_retirement", self.queue)
        monkeypatch.setattr(source_push, "retire", self.retire_push)
        monkeypatch.setattr(httpx.AsyncClient, "send", self.forbid_http)
        monkeypatch.setattr(connections, "unseal", self.forbid_decryption)

    @asynccontextmanager
    async def lease(self, *_args: Any, **_kw: Any):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    async def swap(self, _ch: Any, key: str, previous: str | None, value: str, _ttl: int):
        current = self.fences.get(key)
        if current == previous:
            self.fences[key] = value
            self.events.append(("fence", value))
            return value
        return current

    async def fence(self, _ch: Any, key: str):
        return self.fences.get(key)

    async def get(self, org: str, subject: str, kind: str, **_kw: Any):
        return self.accounts.get((org, subject, kind))

    async def put(self, account: ConnectedAccount, **_kw: Any):
        if self.fail_next_account_write:
            self.fail_next_account_write = False
            raise RuntimeError("account write lost")
        self.accounts[account.org, account.subject, account.kind] = account
        self.events.append(("account", account.kind))

    async def owned(self, subjects: list[str], *, org: str | None = None, **_kw: Any):
        return [
            account
            for account in self.accounts.values()
            if account.subject in subjects and (org is None or account.org == org)
        ]

    async def erase(self, org: str, subjects: list[str], *, kind: str, **_kw: Any):
        for key in list(self.accounts):
            if key[0] == org and key[1] in subjects and key[2] == kind:
                del self.accounts[key]
        self.events.append(("erase", kind))

    async def query_one(self, query: str, params: dict[str, Any]):
        assert "SELECT count() AS n FROM loom.connected_accounts WHERE" in query
        assert "FINAL" not in query
        rows = [
            account
            for key, account in self.accounts.items()
            if key == (params["org"], params["subject"], params["kind"])
        ]
        keep = params.get("keep")
        return {
            "n": sum(
                not (
                    account.connection_id == keep
                    and not account.active
                    and not account.account_id
                    and not account.workspace_id
                    and not account.account_email_enc
                    and not account.credential_enc
                    and not account.scopes
                    and not account.refresh_id
                    and account.connected_at is None
                )
                for account in rows
            )
        }

    async def connected_sources(self, _org: str, _subject: str):
        return self.stale_sources if self.stale_sources is not None else list(self.sources.values())

    async def latest_source(self, _org: str, _namespace: str, name: str):
        return self.sources[name]

    async def insert_source(self, source: KnowledgeSource):
        self.sources[source.name] = source
        self.events.append(("source", source.source_id))

    async def queue(self, source: KnowledgeSource):
        self.events.append(("retire", source.source_id))
        return True

    async def retire_push(self, source: KnowledgeSource):
        self.events.append(("push", source.source_id))

    async def forbid_http(self, *_args: Any, **_kw: Any):
        raise AssertionError("local retirement cannot dispatch provider HTTP")

    def forbid_decryption(self, *_args: Any, **_kw: Any):
        raise AssertionError("local retirement cannot decrypt legacy credentials")


async def _stop(world: _World, kind: str = "gdrive") -> None:
    async with connections.account_lease("team", "person", kind):
        await connections._stop_locked(world.accounts["team", "person", kind])


@pytest.mark.asyncio
async def test_stop_queues_source_retirement_before_inactivation_without_native_execution(
    monkeypatch: pytest.MonkeyPatch,
):
    world = _World(monkeypatch)
    previous = world.accounts["team", "person", "gdrive"].connection_id
    await _stop(world)
    account = world.accounts["team", "person", "gdrive"]
    assert account.connection_id != previous
    assert world.fences[connections._fence_key(account.connection_id)] == account.connection_id
    assert world.events.index(("retire", "source-gdrive")) < world.events.index(
        ("source", "source-gdrive")
    )
    assert not account.active and account.credential_enc == account.account_email_enc == ""
    assert world.sources["gdrive"].pending_delete and not world.sources["gdrive"].active
    assert world.sources["gdrive"].credential_enc == world.sources["gdrive"].secret_enc == ""


@pytest.mark.asyncio
async def test_subject_revocation_stops_every_local_account_and_source_without_native_execution(
    monkeypatch: pytest.MonkeyPatch,
):
    world = _World(monkeypatch, ("gdrive", "notion"))
    await connections.stop_subjects(["person"], org="team")
    assert all(not a.active and not a.credential_enc for a in world.accounts.values())
    assert all(not s.active and s.pending_delete for s in world.sources.values())
    assert {kind for event, kind in world.events if event == "erase"} == {"gdrive", "notion"}


@pytest.mark.asyncio
async def test_disconnect_retry_repairs_cleanup_after_generation_fence_outlives_failed_row_write(
    monkeypatch: pytest.MonkeyPatch,
):
    world = _World(monkeypatch)
    account = world.accounts["team", "person", "gdrive"]
    world.fail_next_account_write = True
    with pytest.raises(RuntimeError, match="account write lost"):
        await _stop(world)
    assert world.fences[connections._fence_key(account.connection_id)] != account.connection_id
    assert not any(event[0] == "provider" for event in world.events)
    await _stop(world)
    assert not world.accounts["team", "person", "gdrive"].active
    assert not world.sources["gdrive"].active and world.sources["gdrive"].pending_delete
    assert world.accounts["team", "person", "gdrive"].credential_enc == ""


@pytest.mark.asyncio
async def test_stale_account_row_cannot_stop_a_newer_live_connection(
    monkeypatch: pytest.MonkeyPatch,
):
    world = _World(monkeypatch)
    account = world.accounts["team", "person", "gdrive"]
    key = connections._fence_key(account.connection_id)
    replacement = account.connection_id.partition("/")[0] + "/1" + "b" * 31
    world.fences[key] = replacement
    with pytest.raises(connections.ValidationError):
        await _stop(world)
    assert world.fences[key] == replacement
    assert world.accounts["team", "person", "gdrive"] is account
    assert world.sources["gdrive"].active
    assert world.events == []
