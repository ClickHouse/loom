"""Owner isolation, guarded commits and complete local disconnect without source I/O."""

from __future__ import annotations

import asyncio
import json
import socket
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from pydantic import SecretStr
from pydantic import ValidationError as ModelError

from server.auth import reader_identity
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.clients.mcp_source import MCPDiscovery, MCPSourceClient
from server.core import secrets_at_rest
from server.core.errors import LoomServerError
from server.knowledge.api.models_mcp_connections import (
    MCPConnectInput,
    MCPInspectInput,
    MCPListInput,
    MCPSlotInput,
)
from server.knowledge.api.ops_mcp_connections import OPERATIONS
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import mcp_connections as app
from server.knowledge.app import mcp_profiles
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.mcp_sources import decode_envelope, encode_envelope
from server.knowledge.store import connected_accounts as store
from server.operations.base import Role
from server.settings import settings

TOKEN = "synthetic-upstream-secret"


def caller(subject: str = "person-a", org: str = "org-a") -> AuthInfo:
    return AuthInfo(
        subject, org, ("personal",), (Role.VIEWER,), False, issued_at=1000, session_id="session"
    )


def request(**changes: Any) -> MCPConnectInput:
    return MCPConnectInput(
        org="org-a", profile_id="docs", request_id=str(uuid4()), bearer=SecretStr(TOKEN), **changes
    )


class World:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], ConnectedAccount] = {}
        self.history: list[ConnectedAccount] = []
        self.fences: dict[str, str] = {}
        self.owners: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.slots: dict[tuple[str, str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        self.held_owners: set[str] = set()
        self.held_slots: set[tuple[str, str, str]] = set()
        self.events: list[str] = []
        self.ch = object()
        self.fleet = True
        self.standing = True
        self.fail_write = ""
        self.remote_hook: Any = None
        self.remote_cancel = False
        self.remote_entered = asyncio.Event()
        self.closed = 0
        self.resources = True
        self.census = 0
        self.settings = SimpleNamespace(
            knowledge_source_self_service_enabled=True,
            knowledge_mcp_connections_max_per_person=32,
            knowledge_connected_stop_timeout_s=3,
            trusted_issuers=lambda: {"unit-loom"},
            auth_actually_required=lambda: True,
        )
        self.configure()

    def configure(self, *, auth: str = "bearer", **changes: Any) -> None:
        config = {
            "endpoint": "https://docs.example/mcp",
            "bearer_audience": "https://docs.example/audience" if auth == "bearer" else None,
        }
        row = {
            "id": "docs",
            "label": "Operator label",
            "auth": auth,
            "allowed_orgs": ["org-a", "org-b"],
            "config": config,
        }
        row.update(changes)
        self.settings.knowledge_source_mcp_profiles_json = json.dumps(
            {"version": 1, "profiles": [row]}
        )

    async def get(self, org: str, owner: str, slot: str, **kwargs: Any) -> ConnectedAccount | None:
        await asyncio.sleep(0)
        return self.rows.get((org, owner, slot))

    async def put(self, row: ConnectedAccount, **kwargs: Any) -> None:
        key = row.org, row.subject, row.kind
        assert key in self.held_slots and row.subject in self.held_owners
        if self.fail_write == "before":
            raise RuntimeError(TOKEN)
        self.rows[key] = row
        self.history.append(row)
        self.events.append("put")
        if self.fail_write == "after":
            raise RuntimeError(TOKEN)
        if self.fail_write == "cancel":
            raise asyncio.CancelledError

    async def erase(self, org: str, subjects: list[str], *, kind: str, **kwargs: Any) -> None:
        assert len(subjects) == 1 and (org, subjects[0], kind) in self.held_slots
        self.history = [
            r for r in self.history if (r.org, r.subject, r.kind) != (org, subjects[0], kind)
        ]
        self.rows.pop((org, subjects[0], kind), None)
        self.events.append("erase")

    async def count(self, org: str, owner: str, *, maximum: int, **kwargs: Any) -> int:
        assert owner in self.held_owners
        await asyncio.sleep(0)
        return min(
            sum(r.active for r in self.rows.values() if (r.org, r.subject) == (org, owner)),
            maximum + 1,
        )

    async def page(
        self, org: str, owner: str, *, after: str, limit: int, **kwargs: Any
    ) -> list[ConnectedAccount]:
        assert 1 <= limit <= 101
        return sorted(
            [
                r
                for r in self.rows.values()
                if (r.org, r.subject) == (org, owner) and r.kind > after
            ],
            key=lambda r: r.kind,
        )[:limit]

    @asynccontextmanager
    async def owner_lease(self, owners: list[str]) -> AsyncIterator[None]:
        assert len(owners) == 1
        owner = owners[0]
        async with self.owners[owner]:
            self.held_owners.add(owner)
            try:
                yield
            finally:
                self.held_owners.remove(owner)

    @asynccontextmanager
    async def slot_lease(
        self, org: str, owner: str, slot: str, **kwargs: Any
    ) -> AsyncIterator[None]:
        assert owner in self.held_owners
        key = org, owner, slot
        async with self.slots[key]:
            self.held_slots.add(key)
            try:
                yield
            finally:
                self.held_slots.remove(key)

    async def claim_get(self, ch: Any, key: str) -> str | None:
        assert ch is self.ch
        return self.fences.get(key)

    async def claim_swap(
        self, ch: Any, key: str, previous: str | None, current: str, expiry: Any
    ) -> str | None:
        assert ch is self.ch
        if self.fences.get(key) == previous:
            self.fences[key] = current
        return self.fences.get(key)

    async def ready(self, ch: Any, migration: int) -> bool:
        assert ch is self.ch and migration == 116
        return self.fleet

    async def canonical(self, org: str, owner: str) -> str:
        return owner

    async def identity(self, org: str, owner: str) -> SimpleNamespace | None:
        return SimpleNamespace(subject=owner, emails=()) if self.standing else None

    async def spellings(self, org: str, owner: str) -> list[str]:
        return [owner]

    async def active(self, org: str, owners: list[str]) -> bool:
        return self.standing

    async def managed(self, *args: Any, **kwargs: Any) -> bool:
        return False

    async def cutoffs(self, keys: list[str]) -> dict[str, Any]:
        return {}

    async def sources(self, org: str, owner: str) -> list[Any]:
        return []

    async def erased(self, org: str, owner: str, kind: str, *, keep_connection: str | None) -> None:
        assert all(
            not r.credential_enc
            for r in self.history
            if (r.org, r.subject, r.kind) == (org, owner, kind)
        )
        assert self.rows[(org, owner, kind)].active is False
        assert self.rows[(org, owner, kind)].connection_id == keep_connection
        self.census += 1


def no_io(*args: Any, **kwargs: Any) -> Any:
    pytest.fail("unexpected network, database or native provider path")


def patch_auth(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    monkeypatch.setattr(accounts, "settings", world.settings)
    monkeypatch.setattr(reader_identity, "settings", world.settings)
    monkeypatch.setattr(accounts.aliases, "canonical_subject", world.canonical)
    monkeypatch.setattr(accounts.aliases, "verified_spellings", world.spellings)
    monkeypatch.setattr(accounts, "resolve_reader", world.identity)
    monkeypatch.setattr(accounts.store, "account_active", world.active)
    monkeypatch.setattr(accounts.scim, "refuses_subject", world.managed)
    monkeypatch.setattr(accounts.revocation, "cutoffs", world.cutoffs)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    from server.erasure.app import connected_accounts as erasure

    state = World()
    patch_auth(monkeypatch, state)
    monkeypatch.setattr(app, "settings", state.settings)
    monkeypatch.setattr(mcp_profiles, "settings", state.settings)
    monkeypatch.setattr(settings, "jwt_secret", "unit-connection-sealing-key")
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    monkeypatch.setattr(httpx, "AsyncClient", no_io)
    monkeypatch.setattr(socket, "socket", no_io)
    monkeypatch.setattr(socket, "getaddrinfo", no_io)
    monkeypatch.setattr(accounts, "client", no_io)
    monkeypatch.setattr(app, "get_client", lambda: state.ch)
    monkeypatch.setattr(accounts, "get_client", lambda: state.ch)
    monkeypatch.setattr(store, "get", state.get)
    monkeypatch.setattr(store, "put", state.put)
    monkeypatch.setattr(store, "erase", state.erase)
    monkeypatch.setattr(store, "mcp_owner_page", state.page)
    monkeypatch.setattr(store, "mcp_owner_active_count", state.count)
    monkeypatch.setattr(accounts, "account_lease", state.slot_lease)
    monkeypatch.setattr(app.scim, "subject_leases", state.owner_lease)
    monkeypatch.setattr(accounts.claims, "get", state.claim_get)
    monkeypatch.setattr(accounts.claims, "swap", state.claim_swap)
    monkeypatch.setattr(app.machines, "fleet_at", state.ready)
    monkeypatch.setattr(accounts.source_store, "connected_sources", state.sources)
    monkeypatch.setattr(erasure, "require_erased", state.erased)
    token = current_auth_var.set(caller())
    try:
        yield state
    finally:
        current_auth_var.reset(token)


async def connect(world: World, input: MCPConnectInput | None = None):
    return await app.handle_mcp_connect(input or request())


@pytest.mark.asyncio
async def test_connect_seals_exact_binding_without_opening_a_client(world: World) -> None:
    result = await connect(world)
    row = next(iter(world.rows.values()))
    envelope = decode_envelope(
        secrets_at_rest.unseal(accounts.CREDENTIAL_PURPOSE, row.credential_enc)
    )
    assert (envelope.org, envelope.owner, envelope.slot, envelope.generation) == (
        "org-a",
        "person-a",
        row.kind,
        row.connection_id,
    )
    assert envelope.bearer is not None and envelope.bearer.get_secret_value() == TOKEN
    assert (
        envelope.endpoint == "https://docs.example/mcp"
        and envelope.audience == "https://docs.example/audience"
    )
    assert row.account_id == row.workspace_id == "" and not row.scopes
    assert result.connection.state == "configured"
    assert (
        result.connection.compatibility == "unchecked"
        and not result.connection.remote_identity_verified
    )
    assert TOKEN not in result.model_dump_json() and TOKEN not in repr(row)
    assert not world.held_owners and not world.held_slots


@pytest.mark.asyncio
async def test_anonymous_profile_accepts_no_bearer(world: World) -> None:
    world.configure(auth="none")
    with pytest.raises(LoomServerError):
        await connect(world)
    assert not world.rows
    input = request().model_copy(update={"bearer": None})
    assert (await connect(world, input)).connection.state == "configured"


@pytest.mark.asyncio
async def test_same_nonce_is_scoped_by_owner_and_org(world: World) -> None:
    first_input = request()
    first = await connect(world, first_input)
    current_auth_var.set(caller("person-b"))
    second = await connect(world, first_input)
    current_auth_var.set(caller("person-a", "org-b"))
    third = await connect(world, first_input.model_copy(update={"org": "org-b"}))
    assert len({first.connection.slot, second.connection.slot, third.connection.slot}) == 3
    assert len(world.rows) == 3


@pytest.mark.asyncio
async def test_successful_retry_does_not_replace_generation_or_credential(world: World) -> None:
    input = request()
    first = await connect(world, input)
    row = next(iter(world.rows.values()))
    second = await connect(world, input)
    assert first == second and len(world.history) == 1
    assert next(iter(world.rows.values())).credential_enc == row.credential_enc
    for change in ({"bearer": SecretStr("changed")}, {"label": "Changed"}, {"profile_id": "other"}):
        with pytest.raises(LoomServerError):
            await connect(world, input.model_copy(update=change))
    assert len(world.history) == 1


@pytest.mark.asyncio
async def test_fresh_slots_share_owner_quota_barrier(world: World) -> None:
    world.settings.knowledge_mcp_connections_max_per_person = 1
    results = await asyncio.gather(connect(world), connect(world), return_exceptions=True)
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert len(world.rows) == 1
    assert any("QUOTA" in str(result) for result in results if isinstance(result, BaseException))
    assert not world.held_owners and not world.held_slots


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["policy", "fleet", "standing", "org"])
async def test_enrollment_refuses_before_effects(world: World, mode: str) -> None:
    if mode == "policy":
        world.settings.knowledge_source_self_service_enabled = False
    elif mode == "fleet":
        world.fleet = False
    elif mode == "standing":
        world.standing = False
    else:
        current_auth_var.set(caller(org="org-b"))
    with pytest.raises(LoomServerError):
        await connect(world)
    assert not world.history and not world.fences


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "after", "cancel"])
async def test_uncertain_save_is_inspectable_and_never_silently_replaced(
    world: World, when: str
) -> None:
    input = request()
    world.fail_write = when
    with pytest.raises((LoomServerError, asyncio.CancelledError)) as caught:
        await connect(world, input)
    assert TOKEN not in str(caught.value)
    world.fail_write = ""
    result = await app.handle_mcp_inspect(MCPInspectInput(org="org-a", request_id=input.request_id))
    if when == "before":
        assert not result.found and result.reconciliation == "incomplete"
        with pytest.raises(LoomServerError, match="INCOMPLETE"):
            await connect(world, input)
        assert not world.rows
    else:
        assert result.found and result.reconciliation == "present"
        saved = next(iter(world.rows.values()))
        assert (await connect(world, input)).connection.generation == saved.connection_id
        assert len(world.history) == 1
    assert not world.held_owners and not world.held_slots


@pytest.mark.asyncio
async def test_fence_only_partial_save_refuses_an_alternate_generation(world: World) -> None:
    input = request()
    slot = app._slot("org-a", "person-a", input.request_id)
    world.fences[accounts._fence_key(slot[4:] + "/" + "0" * 32)] = slot[4:] + "/1" + "d" * 31
    with pytest.raises(LoomServerError, match="INCOMPLETE"):
        await connect(world, input)
    result = await app.handle_mcp_inspect(MCPInspectInput(org="org-a", request_id=input.request_id))
    assert not result.found and result.reconciliation == "incomplete" and not world.rows


@pytest.mark.asyncio
async def test_local_jwts_including_expired_are_never_forwarded(world: World) -> None:
    for claims in ({"iss": "unit-loom"}, {"iss": "unit-loom", "exp": 1}):
        token = jwt.encode(claims, "unit-token-signing-key-that-is-long-enough", algorithm="HS256")
        with pytest.raises(LoomServerError, match="LOCAL_CREDENTIAL"):
            await connect(world, request().model_copy(update={"bearer": SecretStr(token)}))
    assert not world.rows


@pytest.mark.asyncio
async def test_list_inspect_and_disconnect_are_owner_scoped_and_policy_independent(
    world: World,
) -> None:
    own = (await connect(world)).connection.slot
    current_auth_var.set(caller("person-b"))
    foreign = (await connect(world)).connection.slot
    current_auth_var.set(caller())
    world.settings.knowledge_source_self_service_enabled = False
    listed = await app.handle_mcp_list(MCPListInput(org="org-a"))
    assert [entry.slot for entry in listed.connections] == [own]
    hidden = await app.handle_mcp_inspect(MCPInspectInput(org="org-a", slot=foreign))
    assert not hidden.found and hidden.reconciliation == "absent"
    assert not (
        await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=foreign))
    ).disconnected
    ended = await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=own))
    assert ended.disconnected and not ended.upstream_revoked
    assert world.census == 1 and world.events[-2:] == ["erase", "put"]
    assert not world.rows[("org-a", "person-a", own)].credential_enc
    assert world.rows[("org-a", "person-b", foreign)].active
    world.settings.knowledge_source_self_service_enabled = True
    assert (
        await app.handle_mcp_inspect(MCPInspectInput(org="org-a", slot=own))
    ).connection.state == "stopped"


@pytest.mark.asyncio
async def test_disconnect_runs_complete_stop_again_after_census_failure(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.erasure.app import connected_accounts as erasure

    slot = (await connect(world)).connection.slot

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(erasure, "require_erased", fail)
    with pytest.raises(LoomServerError):
        await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=slot))
    assert not world.rows[("org-a", "person-a", slot)].active
    monkeypatch.setattr(erasure, "require_erased", world.erased)
    assert (await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=slot))).disconnected
    assert world.census == 1


@pytest.mark.asyncio
async def test_stopped_nonce_never_revives(world: World) -> None:
    input = request()
    slot = (await connect(world, input)).connection.slot
    await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=slot))
    with pytest.raises(LoomServerError):
        await connect(world, input)
    assert not world.rows[("org-a", "person-a", slot)].active


def mock_protocol(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    async def enter(client: MCPSourceClient) -> MCPSourceClient:
        assert not world.held_owners and not world.held_slots
        await client.validate_access()
        return client

    async def exit(client: MCPSourceClient, *args: Any) -> None:
        world.closed += 1

    async def discover(client: MCPSourceClient) -> MCPDiscovery:
        await client.validate_access()
        world.remote_entered.set()
        if world.remote_cancel:
            await asyncio.Future()
        if world.remote_hook is not None:
            world.remote_hook()
        await client.validate_access()
        return MCPDiscovery(world.resources, ())

    monkeypatch.setattr(MCPSourceClient, "__aenter__", enter)
    monkeypatch.setattr(MCPSourceClient, "__aexit__", exit)
    monkeypatch.setattr(MCPSourceClient, "discover", discover)


@pytest.mark.asyncio
async def test_discover_uses_real_factory_binding_and_returns_only_normalized_capabilities(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = (await connect(world)).connection.slot
    mock_protocol(monkeypatch, world)
    result = await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot))
    assert (
        result.resources
        and result.supported_reads
        and not result.mapped_read
        and not result.mapped_search
    )
    assert not result.remote_identity_verified and world.closed == 1
    assert TOKEN not in result.model_dump_json() and "endpoint" not in result.model_dump_json()
    world.resources = False
    result = await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot))
    assert not result.supported_reads


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift", ["owner", "org", "generation", "endpoint", "audience", "profile", "digest"]
)
async def test_drift_refuses_before_entering_remote_context(
    world: World, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    slot = (await connect(world)).connection.slot
    key = "org-a", "person-a", slot
    row = world.rows[key]
    envelope = decode_envelope(
        secrets_at_rest.unseal(accounts.CREDENTIAL_PURPOSE, row.credential_enc)
    )
    changes = {
        "owner": {"owner": "person-b"},
        "org": {"org": "org-b"},
        "generation": {"generation": "a" * 32 + "/1" + "b" * 31},
        "endpoint": {"endpoint": "https://other.example/mcp"},
        "audience": {"audience": "https://docs.example/other"},
        "profile": {"profile_id": "other"},
        "digest": {"profile_sha256": "0" * 64},
    }
    envelope = envelope.model_copy(update=changes[drift])
    world.rows[key] = replace(
        row,
        credential_enc=secrets_at_rest.seal(accounts.CREDENTIAL_PURPOSE, encode_envelope(envelope)),
    )
    monkeypatch.setattr(MCPSourceClient, "__aenter__", no_io)
    with pytest.raises(LoomServerError):
        await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["standing", "fleet", "profile", "stopped", "generation"])
async def test_mandatory_validator_rechecks_changes_around_remote_work(
    world: World, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    slot = (await connect(world)).connection.slot
    key = "org-a", "person-a", slot

    def mutate() -> None:
        if change == "standing":
            world.standing = False
        elif change == "fleet":
            world.fleet = False
        elif change == "profile":
            world.configure(allowed_orgs=[])
        elif change == "stopped":
            world.rows[key] = replace(world.rows[key], active=False)
        else:
            world.rows[key] = replace(
                world.rows[key],
                connection_id=accounts._next_connection(world.rows[key].connection_id),
            )

    mock_protocol(monkeypatch, world)
    world.remote_hook = mutate
    with pytest.raises(LoomServerError):
        await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot))
    assert world.closed == 1


@pytest.mark.asyncio
async def test_cancelled_discovery_closes_context_and_releases_no_writer_lease(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = (await connect(world)).connection.slot
    mock_protocol(monkeypatch, world)
    world.remote_cancel = True
    task = asyncio.create_task(app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot)))
    await world.remote_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done() and world.closed == 1 and not world.held_owners and not world.held_slots


@pytest.mark.asyncio
async def test_common_cursors_bind_owner_org_and_limit(world: World) -> None:
    await connect(world)
    await connect(world)
    first = await app.handle_mcp_list(MCPListInput(org="org-a", limit=1))
    assert first.next_cursor
    second = await app.handle_mcp_list(MCPListInput(org="org-a", limit=1, cursor=first.next_cursor))
    assert second.connections[0].slot != first.connections[0].slot and second.next_cursor is None
    for org, owner, limit in (
        ("org-a", "person-b", 1),
        ("org-b", "person-a", 1),
        ("org-a", "person-a", 2),
    ):
        current_auth_var.set(caller(owner, org))
        with pytest.raises(LoomServerError):
            await app.handle_mcp_list(MCPListInput(org=org, limit=limit, cursor=first.next_cursor))


def test_credentials_and_labels_are_omitted_from_audits_and_repr() -> None:
    input = request().model_copy(update={"label": "owner-private-label"})
    assert TOKEN not in repr(input) and "owner-private-label" not in repr(input)
    assert "bearer" not in input.model_dump()
    schema = MCPConnectInput.model_json_schema()
    secret = next(x for x in schema["properties"]["bearer"]["anyOf"] if x.get("type") == "string")
    assert secret["format"] == "password" and secret["writeOnly"]
    connect_op = next(op for op in OPERATIONS if op.name.endswith("mcp_connect"))
    assert connect_op.audit and connect_op.writes and not connect_op.idempotent
    output = SimpleNamespace(connection=SimpleNamespace(slot="mcp:" + "a" * 32))
    assert connect_op.audit_subject is not None
    audit = connect_op.audit_subject(input, output)
    assert audit == {"slot": output.connection.slot, "profile_id": "docs"}
    assert TOKEN not in json.dumps(audit) and "owner-private-label" not in json.dumps(audit)
    for field in ("owner", "endpoint", "audience", "tools", "headers"):
        with pytest.raises(ModelError):
            MCPConnectInput(**{**input.model_dump(), field: "injected"})


@pytest.mark.asyncio
async def test_store_queries_are_bounded_owner_scoped_and_parameterized() -> None:
    class CH:
        async def query_all(self, sql: str, *, params: dict[str, Any]):
            assert (
                " FINAL " in sql
                and "org = {org:String}" in sql
                and "subject = {subject:String}" in sql
            )
            assert "match(kind, {slot_pattern:String})" in sql and "LIMIT {limit:UInt32}" in sql
            assert "credential_enc" in sql and "ORDER BY kind" in sql
            assert params == {
                "org": "owner'org",
                "subject": "person",
                "slot_pattern": r"^mcp:[0-9a-f]{32}$",
                "after": "",
                "limit": 2,
            }
            return []

        async def query_one(self, sql: str, *, params: dict[str, Any]):
            assert "count()" in sql and "active = 1" in sql and "LIMIT {limit:UInt32}" in sql
            assert "credential_enc" not in sql and params["limit"] == 33
            assert params["org"] == "owner'org" and params["subject"] == "person"
            return {"n": 32}

    assert await store.mcp_owner_page("owner'org", "person", limit=2, ch=CH()) == []
    assert await store.mcp_owner_active_count("owner'org", "person", maximum=32, ch=CH()) == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["policy", "standing", "fleet", "profile"])
async def test_commit_rechecks_inside_the_owner_barrier(
    world: World, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    @asynccontextmanager
    async def barrier(owners: list[str]) -> AsyncIterator[None]:
        async with world.owner_lease(owners):
            if change == "policy":
                world.settings.knowledge_source_self_service_enabled = False
            elif change == "standing":
                world.standing = False
            elif change == "fleet":
                world.fleet = False
            else:
                world.configure(allowed_orgs=[])
            yield

    monkeypatch.setattr(app.scim, "subject_leases", barrier)
    with pytest.raises(LoomServerError):
        await connect(world)
    assert not world.history and not world.fences


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "inspect", "discover", "disconnect"])
async def test_unexpected_store_scope_never_reveals_foreign_metadata_or_stops_it(
    world: World, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    own_slot = (await connect(world)).connection.slot
    current_auth_var.set(caller("person-b"))
    foreign_slot = (await connect(world)).connection.slot
    foreign = world.rows[("org-a", "person-b", foreign_slot)]
    current_auth_var.set(caller())

    async def wrong_get(*args: Any, **kwargs: Any):
        return foreign

    async def wrong_page(*args: Any, **kwargs: Any):
        return [foreign]

    monkeypatch.setattr(store, "get", wrong_get)
    monkeypatch.setattr(store, "mcp_owner_page", wrong_page)
    with pytest.raises(LoomServerError, match="^MCP_CONNECTION_UNAVAILABLE$"):
        if operation == "list":
            await app.handle_mcp_list(MCPListInput(org="org-a"))
        elif operation == "inspect":
            await app.handle_mcp_inspect(MCPInspectInput(org="org-a", slot=own_slot))
        elif operation == "discover":
            await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=own_slot))
        else:
            await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=own_slot))
    assert world.rows[("org-a", "person-b", foreign_slot)].active
    assert world.census == 0


@pytest.mark.asyncio
async def test_approval_drift_is_visible_local_metadata_and_does_not_disable_removal(
    world: World,
) -> None:
    slot = (await connect(world)).connection.slot
    world.configure(allowed_orgs=[])
    inspected = await app.handle_mcp_inspect(MCPInspectInput(org="org-a", slot=slot))
    assert inspected.connection is not None and inspected.connection.state == "approval_drift"
    assert (
        inspected.connection.profile_id == "docs"
        and inspected.connection.compatibility == "unchecked"
    )
    with pytest.raises(LoomServerError):
        await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot))
    assert (await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=slot))).disconnected


@pytest.mark.asyncio
async def test_unreadable_envelope_is_unavailable_but_complete_stop_still_works(
    world: World,
) -> None:
    slot = (await connect(world)).connection.slot
    key = "org-a", "person-a", slot
    world.rows[key] = replace(world.rows[key], credential_enc="unreadable")
    inspected = await app.handle_mcp_inspect(MCPInspectInput(org="org-a", slot=slot))
    assert inspected.connection is not None and inspected.connection.state == "unavailable"
    assert inspected.connection.profile_id is None and inspected.connection.label is None
    with pytest.raises(LoomServerError):
        await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot))
    assert (await app.handle_mcp_disconnect(MCPSlotInput(org="org-a", slot=slot))).disconnected
    assert world.census == 1


@pytest.mark.asyncio
async def test_fence_missing_after_save_is_not_reported_usable(world: World) -> None:
    slot = (await connect(world)).connection.slot
    world.fences.clear()
    result = await app.handle_mcp_inspect(MCPInspectInput(org="org-a", slot=slot))
    assert result.reconciliation == "incomplete" and result.connection.state == "unavailable"
    with pytest.raises(LoomServerError):
        await app.handle_mcp_discover(MCPSlotInput(org="org-a", slot=slot))


@pytest.mark.asyncio
async def test_missing_or_service_identity_never_enrolls(world: World) -> None:
    for auth in (None, replace(caller(), is_service_account=True)):
        current_auth_var.set(auth)
        with pytest.raises(LoomServerError):
            await connect(world)
    assert not world.rows and not world.fences


@pytest.mark.asyncio
async def test_malformed_request_id_returns_fixed_redacted_error(world: World) -> None:
    with pytest.raises(LoomServerError) as caught:
        await connect(world, request().model_copy(update={"request_id": TOKEN}))
    assert str(caught.value) == "MCP_CONNECTION_UNAVAILABLE"
    assert caught.value.__suppress_context__ and TOKEN not in repr(caught.value)
    assert not world.rows


@pytest.mark.asyncio
async def test_uncertain_write_reserves_nonce_before_any_row_becomes_visible(world: World) -> None:
    input = request()
    world.fail_write = "before"
    with pytest.raises(LoomServerError):
        await connect(world, input)
    assert not world.rows
    inspected = await app.handle_mcp_inspect(
        MCPInspectInput(org="org-a", request_id=input.request_id)
    )
    assert inspected.reconciliation == "incomplete"
    world.fail_write = ""
    with pytest.raises(LoomServerError, match="INCOMPLETE"):
        await connect(world, input)
    assert not world.rows


def test_serialized_validation_errors_never_echo_malformed_secret_input() -> None:
    with pytest.raises(ModelError) as caught:
        MCPConnectInput.model_validate(
            {
                "org": "org-a",
                "profile_id": "docs",
                "request_id": "request",
                "bearer": {"mistyped-secret": TOKEN},
                "label": "owner-private-label",
            }
        )
    serialized = caught.value.json(include_url=False)
    assert TOKEN not in serialized
    assert "owner-private-label" not in serialized
    assert "MCP_CONNECTION_INPUT_INVALID" in serialized


def test_unexpected_secret_on_a_read_input_is_also_redacted() -> None:
    with pytest.raises(ModelError) as caught:
        MCPSlotInput.model_validate({"org": "org-a", "slot": "mcp:" + "a" * 32, "bearer": TOKEN})
    assert TOKEN not in caught.value.json(include_url=False)
