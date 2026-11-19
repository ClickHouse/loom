"""Connected reads retain the requester, source and refresh generation across awaits."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.clients import _vendor, connected_oauth
from server.clients._checked_access import CheckedAccess
from server.core import ch, grant_cache
from server.core.errors import AuthInsufficientError, ValidationError
from server.core.secrets_at_rest import seal, unseal
from server.erasure.app import ledger_reads
from server.erasure.store import census
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE, ConnectedAccount, stopped
from server.knowledge.domain.sources import KnowledgeSource
from server.operations.base import Role, grant_rank
from tests.unit.principals import acting_as

_ACCESS = "source-private-access"
_REFRESH = "source-private-refresh"
_ROTATED = "source-private-rotated-refresh"
_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)
_KEY = ("team", "person", "gdrive")


def _auth(**changes: Any) -> AuthInfo:
    now = int(time.time())
    return replace(
        AuthInfo(
            subject="person@example.com",
            org="team",
            namespaces=("shared-space",),
            roles=(Role.WRITER,),
            is_service_account=False,
            session_id="primary-session",
            token_id="primary-token",
            first_party=True,
            issued_at=now - 10,
            expires_at=now + 3600,
        ),
        **changes,
    )


class _World:
    """External account/source rows, Keeper CAS, directory/grant SQL and HTTP."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
        self.account_active = True
        self.scim_ended = False
        self.cutoffs: dict[str, int | None] = {}
        self.grant: Role | None = Role.WRITER
        self.locks: dict[str, asyncio.Lock] = {}
        self.holders: dict[asyncio.Task[Any], int] = {}
        self.fences: dict[str, str] = {}
        self.rows: dict[tuple[str, str, str], ConnectedAccount] = {}
        self.writes: list[ConnectedAccount] = []
        self.refresh_requests: list[httpx.Request] = []
        self.native_requests: list[httpx.Request] = []
        self.pause_refresh = False
        self.refresh_entered = asyncio.Event()
        self.refresh_release = asyncio.Event()
        self.directory_reads = 0
        configured = {
            "auth_required": True,
            "identity_provider": "gateway",
            "cell": "",
            "jwt_secret": "connected-source-authority-test-root",
            "jwt_secret_previous": "",
            "public_base_url": "https://app.example/prefix",
            "connect_gdrive_client_id": "document-client",
            "connect_gdrive_client_secret": "document-client-secret",
            "knowledge_mirrored_sources_enabled": True,
            "grant_cache_ttl_seconds": 0,
        }
        for name, value in configured.items():
            monkeypatch.setattr(accounts.settings, name, value)
        monkeypatch.setattr(ch, "get_client", lambda: self)
        monkeypatch.setattr(accounts, "get_client", lambda: self)
        monkeypatch.setattr(accounts.source_store, "_default_client", lambda: self)
        monkeypatch.setattr(census, "_default_client", lambda: self)
        monkeypatch.setattr(accounts.authz, "directory", lambda: self)
        monkeypatch.setattr(accounts.aliases, "_CACHE", {})
        monkeypatch.setattr(accounts.aliases, "_GENERATION", {})
        monkeypatch.setattr(grant_cache, "_entries", OrderedDict())
        monkeypatch.setattr(grant_cache, "_loading", {})
        monkeypatch.setattr(grant_cache, "_watching", False)
        monkeypatch.setattr(accounts.leases, "cluster_lease", self.lease)
        monkeypatch.setattr(accounts.claims, "get", self.claim_get)
        monkeypatch.setattr(accounts.claims, "swap", self.claim_swap)
        monkeypatch.setattr(accounts.store, "get", self.get)
        monkeypatch.setattr(accounts.store, "put", self.put)
        monkeypatch.setattr(accounts.store, "erase", self.erase)
        monkeypatch.setattr(accounts.store, "account_active", self.active)
        monkeypatch.setattr(accounts.source_store, "get_source", self.get_source)
        monkeypatch.setattr(accounts.source_store, "latest_source", self.get_source)
        monkeypatch.setattr(accounts.source_store, "connected_sources", self.connected_sources)
        monkeypatch.setattr(accounts.scim, "deactivated", self.deactivated)
        monkeypatch.setattr(accounts.revocation, "cutoffs", self.revoked)
        monkeypatch.setattr(ledger_reads.ledger, "open_namespace_erasures", self.erasures)
        monkeypatch.setattr(connected_oauth, "get_client", lambda: http)
        account = ConnectedAccount(
            *_KEY,
            "a" * 32 + "/1" + "b" * 31,
            account_id="provider-person",
            credential_enc=seal(CREDENTIAL_PURPOSE, _REFRESH),
            scopes=_SCOPES,
            active=True,
            connected_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        self.rows[_KEY] = account
        self.fences[accounts._fence_key(account.connection_id)] = account.connection_id
        self.source: KnowledgeSource | None = KnowledgeSource(
            "team",
            "shared-space",
            "documents",
            "gdrive",
            "source-id",
            config={"include": ["file"]},
            items=[{"id": "file", "kind": "file"}],
            access="mirrored",
            credential_mode="connected",
            owner_subject="person",
            connection_id=account.connection_id,
            created_by="audit-attribution-is-not-authority",
        )

    async def query_all(self, query: str, params: dict[str, Any], **_kw: Any):
        assert "FROM loom.namespaces" in query and params["org"] == "team"
        self.directory_reads += 1
        return [
            {
                "namespace": "personal-person",
                "owner_email": "person",
                "contact_email": "person@example.com",
                "password_hash": "verified-password-hash",
                "hosted_domain": "",
                "issuer": "",
            }
        ]

    async def query_one(self, query: str, params: dict[str, Any], **_kw: Any):
        assert params["org"] == "team"
        if "AS grants" in query:
            assert params["sub"] in ("person", "person@example.com")
            rank = grant_rank(self.grant.name.lower()) if self.grant else 0
            return {
                "grants": [("shared-space", rank)],
                "spellings": ["person", "person@example.com"],
                "held": ["person", "person@example.com"],
            }
        if "SELECT role FROM loom.namespace_grants" in query:
            assert params["ns"] == "shared-space" and "person" in params["subs"]
            return {"role": self.grant.name.lower()} if self.grant else None
        if "owner_email = namespace AS claimed" in query:
            assert params["ns"] == "shared-space"
            return {"active": True, "claimed": False}
        assert "SELECT count() AS n FROM loom.connected_accounts WHERE" in query
        assert "FINAL" not in query
        row = self.rows.get((params["org"], params["subject"], params["kind"]))
        return {
            "n": int(
                row is not None
                and not (
                    row.connection_id == params.get("keep")
                    and not row.active
                    and not row.account_id
                    and not row.workspace_id
                    and not row.account_email_enc
                    and not row.credential_enc
                    and not row.scopes
                    and not row.refresh_id
                    and row.connected_at is None
                )
            )
        }

    async def is_restricted(self, org: str, namespace: str) -> bool:
        assert (org, namespace) == ("team", "shared-space")
        return False

    def code_entry(self, _org: str, _namespace: str) -> None:
        return None

    async def active(self, org: str, spellings: list[str]) -> bool:
        assert org == "team" and "person" in spellings
        return self.account_active

    async def deactivated(self, email: str) -> bool:
        assert email == "person@example.com"
        return self.scim_ended

    async def revoked(self, subjects: list[str]) -> dict[str, int | None]:
        return {key: self.cutoffs[key] for key in subjects if key in self.cutoffs}

    async def erasures(self, org: str, **_kw: Any) -> list[dict[str, Any]]:
        assert org == "team"
        return []

    @asynccontextmanager
    async def lease(self, key: str, **_kw: Any) -> AsyncIterator[None]:
        async with self.locks.setdefault(key, asyncio.Lock()):
            task = asyncio.current_task()
            assert task is not None
            self.holders[task] = self.holders.get(task, 0) + 1
            try:
                yield
            finally:
                self.holders[task] -= 1

    async def claim_get(self, _ch: Any, key: str) -> str | None:
        return self.fences.get(key)

    async def claim_swap(
        self, _ch: Any, key: str, previous: str | None, value: str, _expires: int
    ) -> str | None:
        current = self.fences.get(key)
        if current == previous:
            self.fences[key] = value
            return value
        return current

    async def get(self, org: str, subject: str, kind: str, **_kw: Any):
        return self.rows.get((org, subject, kind))

    async def put(self, row: ConnectedAccount, **_kw: Any) -> None:
        assert self.holders.get(asyncio.current_task(), 0)
        self.rows[row.org, row.subject, row.kind] = row
        self.writes.append(row)

    async def erase(self, org: str, subjects: list[str], *, kind: str, **_kw: Any) -> None:
        for key in list(self.rows):
            if key[0] == org and key[1] in subjects and key[2] == kind:
                del self.rows[key]

    async def get_source(self, org: str, namespace: str, name: str, **_kw: Any):
        assert (org, namespace, name) == ("team", "shared-space", "documents")
        return self.source

    async def connected_sources(self, org: str, subject: str, **_kw: Any):
        assert (org, subject) == ("team", "person")
        return [self.source] if self.source is not None else []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert not self.holders.get(asyncio.current_task(), 0)
        if str(request.url) == "https://oauth2.googleapis.com/token":
            self.refresh_requests.append(request)
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            assert form["refresh_token"] == [_REFRESH]
            assert self.rows[_KEY].refresh_id
            self.refresh_entered.set()
            if self.pause_refresh:
                await self.refresh_release.wait()
            return httpx.Response(
                200,
                json={
                    "access_token": _ACCESS,
                    "refresh_token": _ROTATED,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": " ".join(_SCOPES),
                },
            )
        assert str(request.url) == "https://www.googleapis.com/drive/v3/files/file"
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer " + _ACCESS
        self.native_requests.append(request)
        return httpx.Response(200, json={"id": "file"})

    async def entered_or_finished(self, work: asyncio.Task[Any]) -> None:
        entered = asyncio.create_task(self.refresh_entered.wait())
        try:
            done, _ = await asyncio.wait({work, entered}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
                pytest.fail("the refresh did not remain reserved")
        finally:
            if not entered.done():
                entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    async def handle(request: httpx.Request) -> httpx.Response:
        return await state.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        state = _World(monkeypatch, http)
        yield state, http


async def _resolve() -> accounts.SourceAuthority:
    return await accounts.resolve(
        "team", "shared-space", "documents", deadline=time.monotonic() + 60
    )


async def test_canonical_owner_reads_through_a_checked_native_checkout(world) -> None:
    state, http = world
    with acting_as(_auth()):
        authority = await _resolve()
        assert authority.requester == "person" and authority.source.owner_subject == "person"

        async def read(access: CheckedAccess):
            assert isinstance(access, CheckedAccess)
            with pytest.raises(TypeError):
                str(access)
            return await _vendor.bearer_get(
                http, "https://www.googleapis.com/drive/v3/files/file", access, "metadata"
            )

        assert await authority.run(read) == {"id": "file"}
    assert len(state.refresh_requests) == len(state.native_requests) == 1
    assert state.directory_reads > 0 and len(state.writes) == 2
    assert state.writes[0].refresh_id and not state.writes[1].refresh_id
    assert unseal(CREDENTIAL_PURPOSE, state.rows[_KEY].credential_enc) == _ROTATED
    assert _ACCESS not in json.dumps([asdict(row) for row in state.writes], default=str)
    assert _ACCESS not in repr(authority) and _ROTATED not in repr(authority)


@pytest.mark.parametrize("refusal", ("source-id", "owner", "org"))
async def test_resolution_refuses_a_different_source_owner_or_tenant_before_refresh(
    world, refusal: str
) -> None:
    state, _http = world
    assert state.source is not None
    if refusal == "owner":
        state.source = replace(state.source, owner_subject="another-person", created_by="person")
    with acting_as(_auth()):
        with pytest.raises(ValidationError, match="^the connected account is unavailable$"):
            await accounts.resolve(
                "other-team" if refusal == "org" else "team",
                "shared-space",
                "documents",
                deadline=time.monotonic() + 60,
                expected_source_id="replacement-id" if refusal == "source-id" else "",
            )
    assert state.refresh_requests == [] and state.writes == []


@pytest.mark.parametrize("principal", ("service", "delegated"))
async def test_service_or_creator_attribution_cannot_select_the_persons_account(
    world, principal: str
) -> None:
    state, _http = world
    auth = (
        _auth(is_service_account=True)
        if principal == "service"
        else _auth(reads_as_creator=True, creator="person")
    )
    with acting_as(auth), pytest.raises(AuthInsufficientError):
        await _resolve()
    assert state.refresh_requests == [] and state.writes == []


async def test_source_metadata_and_keeper_fence_are_required_before_refresh(world) -> None:
    state, _http = world
    source = state.source
    assert source is not None
    changes = (
        {"access": "namespace"},
        {"credential_mode": "shared"},
        {"active": False},
        {"items": [{"id": "different-file"}]},
        {"config": {"include": ["different-file"]}},
    )
    with acting_as(_auth()):
        for change in changes:
            state.source = replace(source, **change)
            with pytest.raises(ValidationError):
                await accounts.token_for(source, deadline=time.monotonic() + 60)
        state.source = source
        state.fences.clear()
        with pytest.raises(ValidationError):
            await accounts.token_for(source, deadline=time.monotonic() + 60)
    assert state.refresh_requests == [] and state.writes == []


async def test_owner_account_scim_and_outright_revocation_are_live_before_refresh(world) -> None:
    state, _http = world
    with acting_as(_auth()):
        state.account_active = False
        with pytest.raises(ValidationError):
            await _resolve()
        state.account_active = True
        state.scim_ended = True
        with pytest.raises(ValidationError):
            await _resolve()
        state.scim_ended = False
        state.cutoffs["person"] = None
        with pytest.raises(ValidationError):
            await _resolve()
    assert state.refresh_requests == [] and state.writes == []


@pytest.mark.parametrize("change", ("writer-grant", "scim", "auth-object", "source-config"))
async def test_authority_withholds_a_read_when_its_post_call_standing_changes(
    world, change: str
) -> None:
    state, http = world
    auth = _auth()
    with acting_as(auth):
        authority = await _resolve()

        async def read(access: CheckedAccess):
            answer = await _vendor.bearer_get(
                http, "https://www.googleapis.com/drive/v3/files/file", access, "metadata"
            )
            if change == "writer-grant":
                state.grant = Role.VIEWER
            elif change == "scim":
                state.scim_ended = True
            elif change == "auth-object":
                current_auth_var.set(replace(auth))
            else:
                assert state.source is not None
                state.source = replace(state.source, config={"include": ["different-file"]})
            return answer

        with pytest.raises(ValidationError):
            await authority.run(read)
    assert len(state.native_requests) == len(state.refresh_requests) == 1


async def test_an_outstanding_rotation_refuses_a_second_presenter(world) -> None:
    state, _http = world
    state.pause_refresh = True
    source = state.source
    assert source is not None
    with acting_as(_auth()):
        work = asyncio.create_task(accounts.token_for(source, deadline=time.monotonic() + 60))
        try:
            await state.entered_or_finished(work)
            assert state.rows[_KEY].refresh_id
            with pytest.raises(ValidationError):
                await accounts.token_for(source, deadline=time.monotonic() + 60)
            assert len(state.refresh_requests) == 1
            state.refresh_release.set()
            tokens, _account = await work
            assert tokens.access_token == _ACCESS
        finally:
            if not work.done():
                state.source = None
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
    assert not state.rows[_KEY].refresh_id


@pytest.mark.parametrize("replacement", (False, True), ids=("disconnected", "replaced"))
async def test_a_late_rotation_cannot_restore_an_ended_or_replaced_generation(
    world, replacement: bool
) -> None:
    state, _http = world
    state.pause_refresh = True
    source = state.source
    assert source is not None
    with acting_as(_auth()):
        work = asyncio.create_task(accounts.token_for(source, deadline=time.monotonic() + 60))
        try:
            await state.entered_or_finished(work)
            old = state.rows[_KEY]
            generation = accounts._next_connection(old.connection_id, inactive=not replacement)
            ended = (
                replace(
                    old,
                    connection_id=generation,
                    refresh_id="",
                    credential_enc=seal(CREDENTIAL_PURPOSE, "replacement-refresh"),
                )
                if replacement
                else stopped(old, connection_id=generation, at=datetime.now(UTC))
            )
            state.rows[_KEY] = ended
            state.fences[accounts._fence_key(generation)] = generation
            state.source = replace(source, active=False)
            state.refresh_release.set()
            with pytest.raises(ValidationError):
                await work
            assert state.rows[_KEY] == ended
            assert state.fences[accounts._fence_key(generation)] == generation
        finally:
            if not work.done():
                state.source = None
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
    assert len(state.refresh_requests) == 1


async def test_a_changed_rotation_reservation_cannot_be_committed_or_cleared(world) -> None:
    state, _http = world
    state.pause_refresh = True
    source = state.source
    assert source is not None
    with acting_as(_auth()):
        work = asyncio.create_task(accounts.token_for(source, deadline=time.monotonic() + 60))
        try:
            await state.entered_or_finished(work)
            other = replace(state.rows[_KEY], refresh_id="another-reservation")
            state.rows[_KEY] = other
            state.refresh_release.set()
            with pytest.raises(ValidationError):
                await work
            assert state.rows[_KEY] == other
            assert unseal(CREDENTIAL_PURPOSE, other.credential_enc) == _REFRESH
        finally:
            if not work.done():
                state.source = None
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)


async def test_draft_probe_needs_its_current_owner_and_writer_grant_without_a_source_row(
    world,
) -> None:
    state, _http = world
    draft = state.source
    assert draft is not None
    state.source = None
    with acting_as(_auth()):
        access = await accounts.checked_for(draft, deadline=time.monotonic() + 60, pending=True)
        await access.check()
        state.grant = Role.VIEWER
        with pytest.raises(ValidationError):
            await access.check()
    assert len(state.refresh_requests) == 1 and state.native_requests == []


async def test_source_deleted_during_rotation_withholds_delivery_and_keeps_owner_rotation(
    world,
) -> None:
    state, _http = world
    state.pause_refresh = True
    source = state.source
    assert source is not None
    with acting_as(_auth()):
        work = asyncio.create_task(accounts.token_for(source, deadline=time.monotonic() + 60))
        try:
            await state.entered_or_finished(work)
            state.source = None
            state.refresh_release.set()
            with pytest.raises(ValidationError):
                await work
        finally:
            if not work.done():
                state.source = None
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
    retained = state.rows[_KEY]
    assert state.source is None and state.native_requests == []
    assert retained.active and not retained.refresh_id
    assert retained.connection_id == source.connection_id
    assert state.fences[accounts._fence_key(retained.connection_id)] == retained.connection_id
    assert unseal(CREDENTIAL_PURPOSE, retained.credential_enc) == _ROTATED
    assert retained.connected_at is not None and len(state.refresh_requests) == 1
