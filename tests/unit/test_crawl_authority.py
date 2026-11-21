"""Native Drive calls keep source, signer and provider-person authority across awaits."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from server.auth import scim
from server.auth import source_registration as registration
from server.auth.jwt import verify_token
from server.clients import gdrive
from server.clients import google_workspace as google
from server.clients._vendor import CredentialRefused, VendorError
from server.core.errors import ValidationError
from server.core.secrets_at_rest import seal
from server.knowledge.app import crawl_authority, crawl_exclusions, crawl_expansion
from server.knowledge.app import sources as source_ops
from server.knowledge.domain.source_crawl import Person
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import crawl_exclusions as exclusion_store
from server.operations.base import Role
from server.settings import settings
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import _World as _AuthorityWorld
from tests.unit.test_crawl_registration import _token
from tests.unit.test_crawl_signing_authority import key_view as key_view
from tests.unit.test_google_workspace import account as account

pytestmark = [pytest.mark.asyncio, pytest.mark.claim_cap]
_BOUNDS = crawl_authority.Bounds(crawl_expansion.Limits(16384, 10, 10), 60, 0)
_NATIVE_SCIM_READ = scim.deactivated


class _World(_AuthorityWorld):
    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient, account: Any):
        super().__init__(monkeypatch, http)
        self.grant = Role.ADMIN
        self.exclusion_keys: dict[str, dict[str, Any]] = {}
        self.exclusion_rows: dict[tuple[str, str], bool] = {}
        self.provider_calls: list[httpx.Request] = []
        self.claims_sent: list[dict[str, Any]] = []
        self.storage_version = 0
        self.person_erasure_pending = False
        self.ended_people: set[str] = set()
        self.account = account
        self.customer = "workspace"
        self.person_email = "one@example.com"
        self.after_grant: Any = lambda: None
        self.after_native: Any = lambda: None
        monkeypatch.setattr(google, "get_client", lambda: http)
        monkeypatch.setattr(gdrive, "get_client", lambda: http)
        monkeypatch.setattr(crawl_authority, "get_client", lambda: self)
        monkeypatch.setattr(scim, "deactivated", _NATIVE_SCIM_READ)
        monkeypatch.setattr(scim, "get_client", lambda: self)
        service = account[0]
        self.credential = json.dumps(
            {
                "type": "service_account",
                "client_email": service.client_email,
                "private_key": service.private_key,
            }
        )
        self.source = KnowledgeSource(
            org="team",
            namespace="shared-space",
            name="documents",
            kind="gdrive",
            source_id="crawl-source",
            config={"include": ["docs"]},
            items=[{"type": "file", "id": "file"}],
            access="mirrored",
            credential_mode="crawl",
            secret_enc=seal(source_ops._SECRET_PURPOSE, self.credential),
            crawl_options={
                "directory_subject": "directory@example.com",
                "subjects": [{"type": "person", "email": "one@example.com"}],
            },
            crawl_generation="crawl-generation",
            crawl_customer_id="workspace",
            crawl_credential_hash=hashlib.sha256(self.credential.encode()).hexdigest(),
            created_by="audit-attribution-is-not-authority",
        )

    async def query_one(self, query: str, params: dict[str, Any] | None = None, **kwargs: Any):
        if query.startswith("SELECT 1 AS pending FROM loom.scim_users FINAL"):
            assert params is None
            return {"pending": 1} if self.person_erasure_pending else None
        assert params is not None
        if query.startswith("SELECT 1 AS x FROM loom.scim_users FINAL"):
            assert set(params) == {"id"}
            ended = self.ended_people | ({"person@example.com"} if self.scim_ended else set())
            return {"x": 1} if params["id"] in {scim.user_id(email) for email in ended} else None
        if query.startswith("SELECT greatest(") and any(
            f"FROM {table} " in query for table in (exclusion_store.KEYS, exclusion_store.REFUSALS)
        ):
            self.storage_version += 1
            return {"v": self.storage_version}
        if f"FROM {exclusion_store.KEYS} FINAL" in query:
            return self.exclusion_keys.get(params["org"])
        if f"FROM {exclusion_store.REFUSALS} FINAL" in query:
            return next(
                ({"digest": d} for org, d in self.exclusion_rows if org == params["org"]), None
            )
        return await super().query_one(query, params, **kwargs)

    async def query_all(self, query: str, params: dict[str, Any], **kwargs: Any):
        if query.startswith("SELECT digest, excluded FROM loom.knowledge_crawl_exclusions FINAL"):
            return [
                {"digest": d, "excluded": self.exclusion_rows[params["org"], d]}
                for d in params["digests"]
                if (params["org"], d) in self.exclusion_rows
            ]
        if f"FROM {exclusion_store.REFUSALS} FINAL" in query:
            return [
                {"digest": d}
                for d in params["digests"]
                if self.exclusion_rows.get((params["org"], d))
            ]
        return await super().query_all(query, params, **kwargs)

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert self.holders.get(asyncio.current_task(), 0)
        for row in rows:
            if table == exclusion_store.KEYS:
                self.exclusion_keys[row["org"]] = dict(row)
            else:
                assert table == exclusion_store.REFUSALS
                self.exclusion_rows[row["org"], row["digest"]] = row["excluded"]

    async def setup(self) -> None:
        assert self.source is not None
        auth = verify_token(_token())
        with acting_as(auth):
            registrant, approval = await registration.capture(
                auth, crawl_authority.scope(self.source)
            )
            self.source = dataclasses.replace(
                self.source, registrant_subject=registrant, approval_enc=approval
            )
            permit = await registration.current_permit(crawl_authority.scope(self.source))
            await crawl_exclusions.initialize("team", authority=permit, ttl_s=60, wait_s=0, ch=self)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.provider_calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            claims = jwt.decode(
                form["assertion"][0],
                self.account[1],
                algorithms=["RS256"],
                audience="https://oauth2.googleapis.com/token",
            )
            self.claims_sent.append(claims)
            if claims["scope"] in {write.value for write in gdrive.WriteScope}:
                assert claims["sub"] == "directory@example.com"
                return httpx.Response(400, json={"error": "unauthorized_client"})
            drive = claims["sub"] == "one@example.com"
            assert not drive or claims["scope"] == gdrive.ReadScope.DRIVE.value
            assert claims["sub"] == ("one@example.com" if drive else "directory@example.com")
            if drive:
                self.after_grant()
            return httpx.Response(
                200,
                json={
                    "access_token": "private-person-access"
                    if drive
                    else "private-directory-access",
                    "token_type": "Bearer",
                    "expires_in": 60,
                },
            )
        if request.url.host == "admin.googleapis.com":
            assert request.headers["authorization"] == "Bearer private-directory-access"
            key = request.url.path.rsplit("/", 1)[1]
            directory = key == "directory@example.com"
            assert directory or key in {"one@example.com", "provider-one"}
            return httpx.Response(
                200,
                json={
                    "id": "directory-user" if directory else "provider-one",
                    "primaryEmail": "directory@example.com" if directory else self.person_email,
                    "aliases": [],
                    "customerId": self.customer,
                    "suspended": False,
                    "archived": False,
                },
            )
        assert (
            request.url.host == "www.googleapis.com" and request.url.path == "/drive/v3/files/file"
        )
        assert request.headers["authorization"] == "Bearer private-person-access"
        self.native_requests.append(request)
        self.after_native()
        return httpx.Response(200, json={"id": "file", "name": "private-document"})


@pytest.fixture
async def crawl_world(monkeypatch: pytest.MonkeyPatch, account: Any, key_view: list[str]):
    async def handle(request: httpx.Request) -> httpx.Response:
        return await state.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        state = _World(monkeypatch, http, account)
        await state.setup()
        yield state


async def _ready(state: _World):
    assert state.source is not None
    authority = await crawl_authority.resolve(
        state.source, deadline=time.monotonic() + 10, bounds=_BOUNDS
    )
    expanded = await authority.prepare()
    (person,) = expanded.people
    return authority, person


async def test_real_checked_drive_uses_only_the_expanded_provider_person(
    crawl_world: _World,
) -> None:
    authority, person = await _ready(crawl_world)
    access = await authority.drive(person)
    assert await gdrive.file_meta(access, "file") == {"id": "file", "name": "private-document"}
    assert len(crawl_world.native_requests) == 1
    assert {c["sub"] for c in crawl_world.claims_sent} == {
        "directory@example.com",
        "one@example.com",
    }
    assert all(
        c["sub"] not in {"person", "person@example.com", crawl_world.source.created_by}
        for c in crawl_world.claims_sent
    )
    with pytest.raises(TypeError):
        str(access)


async def test_unexpanded_or_email_shaped_loom_subject_cannot_select_delegation(
    crawl_world: _World,
) -> None:
    authority, _person = await _ready(crawl_world)
    before = len(crawl_world.provider_calls)
    with pytest.raises(CredentialRefused):
        await authority.drive(Person("workspace", "person", "person@example.com", ()))
    assert len(crawl_world.provider_calls) == before and not crawl_world.native_requests


async def test_provider_address_rebind_during_grant_stops_before_native_drive(
    crawl_world: _World,
) -> None:
    authority, person = await _ready(crawl_world)
    crawl_world.after_grant = lambda: setattr(crawl_world, "person_email", "renamed@example.com")
    with pytest.raises(CredentialRefused, match="person changed"):
        await authority.drive(person)
    assert not crawl_world.native_requests


@pytest.mark.parametrize("loss", ["grant", "registration", "exclusion"])
async def test_native_response_never_returns_after_current_authority_loss(
    crawl_world: _World, loss: str
) -> None:
    authority, person = await _ready(crawl_world)

    def changed() -> None:
        if loss == "grant":
            crawl_world.grant = Role.WRITER
        elif loss == "registration":
            crawl_world.source = dataclasses.replace(
                crawl_world.source, crawl_generation="replacement"
            )
        else:
            crawl_world.fences[crawl_exclusions._fence("team")] = "blocked:another-erasure"

    crawl_world.after_native = changed
    with pytest.raises((PermissionError, VendorError)):
        await gdrive.file_meta(await authority.drive(person), "file")
    assert len(crawl_world.native_requests) == 1


async def test_non_provider_wait_is_checked_before_result_delivery(crawl_world: _World) -> None:
    authority, person = await _ready(crawl_world)
    entered = []

    async def model_wait(_access: Any) -> str:
        entered.append(True)
        crawl_world.grant = Role.WRITER
        return "private-model-result"

    with pytest.raises(PermissionError):
        await authority.run(person, model_wait)
    assert entered and not crawl_world.native_requests


async def test_modified_selection_without_new_approval_makes_zero_provider_calls(
    crawl_world: _World,
) -> None:
    assert crawl_world.source is not None
    crawl_world.source = dataclasses.replace(
        crawl_world.source, items=[{"type": "file", "id": "other-file"}]
    )
    with pytest.raises(registration.Refused):
        await crawl_authority.resolve(
            crawl_world.source, deadline=time.monotonic() + 10, bounds=_BOUNDS
        )
    assert not crawl_world.provider_calls


async def test_different_sealed_key_cannot_inherit_the_approved_key_hash(
    crawl_world: _World,
) -> None:
    assert crawl_world.source is not None
    crawl_world.source = dataclasses.replace(
        crawl_world.source, secret_enc=seal(source_ops._SECRET_PURPOSE, "different-secret")
    )
    with pytest.raises(CredentialRefused):
        await crawl_authority.resolve(
            crawl_world.source, deadline=time.monotonic() + 10, bounds=_BOUNDS
        )
    assert not crawl_world.provider_calls


async def test_workspace_change_is_refused_before_any_drive_grant(crawl_world: _World) -> None:
    crawl_world.customer = "different-workspace"
    with pytest.raises(CredentialRefused, match="workspace changed"):
        await _ready(crawl_world)
    assert all(c["scope"] != gdrive.ReadScope.DRIVE.value for c in crawl_world.claims_sent)
    assert not crawl_world.native_requests


async def test_ordinary_credential_selection_never_falls_back_to_a_crawl_key(
    crawl_world: _World,
) -> None:
    assert crawl_world.source is not None
    assert source_ops.credential_of(crawl_world.source) == ""
    with pytest.raises(ValidationError):
        await source_ops.credential_for(crawl_world.source, deadline=time.monotonic() + 10)
    assert not crawl_world.provider_calls


async def test_unfinished_person_erasure_refuses_before_unsealing_or_provider_work(
    crawl_world: _World,
) -> None:
    from server.core.errors import BackendUnavailableError

    crawl_world.person_erasure_pending = True
    assert crawl_world.source is not None
    # Even an unusable provider key is never reached before the pending-person gate.
    crawl_world.source = dataclasses.replace(crawl_world.source, secret_enc="unreadable-key")
    with pytest.raises(BackendUnavailableError, match="canonical-person cleanup"):
        await crawl_authority.resolve(
            crawl_world.source, deadline=time.monotonic() + 10, bounds=_BOUNDS
        )
    assert not crawl_world.provider_calls and not crawl_world.native_requests


async def test_retained_native_checkout_stops_when_target_person_is_deactivated(
    crawl_world: _World,
) -> None:
    authority, person = await _ready(crawl_world)
    access = await authority.drive(person)
    before = len(crawl_world.provider_calls)
    crawl_world.ended_people.add(person.email)
    with pytest.raises(CredentialRefused, match="person unavailable"):
        await gdrive.file_meta(access, "file")
    assert len(crawl_world.provider_calls) == before and not crawl_world.native_requests


async def test_configured_crawl_lease_ttl_reaches_the_real_publication_guard(
    crawl_world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from server.core import leases

    monkeypatch.setattr(settings, "knowledge_crawl_lease_ttl_s", 17)
    original = leases.cluster_lease
    acquired = []

    @asynccontextmanager
    async def record(key: str, **kwargs: Any):
        acquired.append((key, kwargs["ttl_s"]))
        async with original(key, **kwargs):
            yield

    @asynccontextmanager
    async def holding(key: str, **kwargs: Any):
        assert key == "knowledge-source:team:shared-space"
        async with crawl_world.lease(key, **kwargs):
            yield True

    monkeypatch.setattr(leases, "cluster_lease", record)
    monkeypatch.setattr(leases, "holding", holding)
    assert crawl_world.source is not None
    authority = await crawl_authority.resolve(
        crawl_world.source,
        deadline=time.monotonic() + 10,
        bounds=crawl_authority.configured_bounds(),
    )
    async with authority.publication_guard():
        assert crawl_world.holders.get(asyncio.current_task(), 0) == 2
    assert (crawl_exclusions._fence("team"), 17) in acquired
    assert not crawl_world.holders.get(asyncio.current_task(), 0)
