"""Browser consent is single-use and cannot restore an ended connection."""

from __future__ import annotations

import asyncio
import json
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.auth.jwt import AuthInfo, mint_token
from server.auth.oauth_store import ClickHouseStore, token_id
from server.clients import connected_oauth as provider_wire
from server.core import cell, ch
from server.core.errors import AuthInsufficientError, ValidationError
from server.core.secrets_at_rest import unseal
from server.erasure.store import census
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import connection_routes as routes
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE, ConnectedAccount
from server.operations.base import Role

_ACCESS = "callback-private-access"
_REFRESH = "callback-private-refresh"
_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
_DRIVE = "https://www.googleapis.com/auth/drive.readonly"
_SCIM_DEACTIVATED = accounts.scim.deactivated


class _UnreadErrorBody(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("provider refusal bodies must not be read")
        yield b""


def _auth(**changes: Any) -> AuthInfo:
    now = int(time.time())
    return replace(
        AuthInfo(
            subject="person",
            org="team",
            namespaces=("shared-space",),
            roles=(Role.WRITER,),
            is_service_account=False,
            session_id="primary-session",
            first_party=True,
            token_id="primary-token",
            issued_at=now - 10,
            expires_at=now + 3600,
        ),
        **changes,
    )


def _request(
    kind: str, state: str, binding: str, code: str = "older-code", *, accept: str = ""
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("app.example", 443),
            "path": f"/v1/connect/{kind}/callback",
            "query_string": urlencode({"state": state, "code": code}).encode(),
            "headers": [
                (b"cookie", f"{routes._cookie(state)}={binding}".encode()),
                (b"accept", accept.encode()),
            ],
        }
    )


def _primary_token() -> str:
    return mint_token(
        subject="person",
        org="team",
        namespaces=["shared-space"],
        roles=["writer"],
        is_service_account=False,
        session_id="primary-session",
        first_party=True,
        ttl_seconds=3600,
        jti="primary-token",
    )


def _start_request(authorization: str | None, *, on_behalf: bool = False) -> Request:
    headers = [(b"host", b"untrusted.example")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if on_behalf:
        headers.append((b"x-loom-on-behalf-of", b"another-person"))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("untrusted.example", 443),
            "path": "/v1/connect/gdrive/start",
            "root_path": "/prefix",
            "query_string": b"",
            "headers": headers,
        }
    )


class _World:
    """External directory, Keeper, account rows, leases and provider transport."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
        self.rows: dict[tuple[str, str, str], ConnectedAccount] = {}
        self.keeper: dict[str, tuple[str, int]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.holders: dict[asyncio.Task[Any], int] = {}
        self.active = True
        self.revocations: dict[str, int | None] = {}
        self.directory_reads = 0
        self.requests: list[httpx.Request] = []
        self.audits: list[dict[str, Any]] = []
        self.pause_exchange = False
        self.invalid_grant = False
        self.http_failure = False
        self.exchange_entered = asyncio.Event()
        self.exchange_release = asyncio.Event()
        self.exchange_active = 0

        configured = {
            "auth_required": True,
            "identity_provider": "gateway",
            "jwt_secret": "connected-account-callback-test-root",
            "jwt_secret_previous": "",
            "public_base_url": "https://app.example/prefix",
            "connect_gdrive_client_id": "document-client",
            "connect_gdrive_client_secret": "document-client-secret",
            "connect_linear_client_id": "tracker-client",
            "connect_linear_client_secret": "tracker-client-secret",
        }
        for name, value in configured.items():
            monkeypatch.setattr(accounts.settings, name, value)
        monkeypatch.setattr(ch, "get_client", lambda: self)
        monkeypatch.setattr(accounts, "get_client", lambda: self)
        monkeypatch.setattr(accounts, "pending_store", ClickHouseStore())
        monkeypatch.setattr(accounts.claims, "get", self.claim_get)
        monkeypatch.setattr(accounts.claims, "swap", self.claim_swap)
        monkeypatch.setattr(accounts.leases, "cluster_lease", self.lease)
        monkeypatch.setattr(accounts.store, "get", self.get)
        monkeypatch.setattr(accounts.store, "put", self.put)
        monkeypatch.setattr(accounts.store, "account_active", self.account_active)
        monkeypatch.setattr(accounts.store, "erase", self.erase)
        monkeypatch.setattr(census, "_default_client", lambda: self)
        monkeypatch.setattr(accounts.source_store, "connected_sources", self.sources)
        monkeypatch.setattr(accounts.scim, "deactivated", self.deactivated)
        monkeypatch.setattr(accounts.revocation, "cutoffs", self.cutoffs)
        monkeypatch.setattr(accounts.audit, "emit", self.audit)
        monkeypatch.setattr(provider_wire, "get_client", lambda: http)

    async def query_all(self, query: str, params: Any = None, **_kw: Any):
        assert "FROM loom.namespaces" in query
        assert params["org"] == "team"
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

    async def account_active(self, org: str, spellings: list[str]) -> bool:
        assert org == "team" and "person" in spellings
        return self.active

    async def deactivated(self, _email: str) -> bool:
        return False

    async def cutoffs(self, subjects: list[str]) -> dict[str, int | None]:
        return {key: self.revocations[key] for key in subjects if key in self.revocations}

    async def claim_get(self, _ch: Any, key: str) -> str | None:
        # Two callbacks may read the same unspent record before its compare-and-set.
        await asyncio.sleep(0)
        record = self.keeper.get(key)
        return record[0] if record is not None and record[1] >= int(time.time()) else None

    async def claim_swap(
        self, _ch: Any, key: str, previous: str | None, value: str, expires_at: int
    ) -> str | None:
        record = self.keeper.get(key)
        current = record[0] if record is not None and record[1] >= int(time.time()) else None
        if current == previous:
            self.keeper[key] = value, expires_at
            return value
        return current

    @asynccontextmanager
    async def lease(self, key: str, **_kw: Any):
        async with self.locks.setdefault(key, asyncio.Lock()):
            task = asyncio.current_task()
            assert task is not None
            self.holders[task] = self.holders.get(task, 0) + 1
            try:
                yield
            finally:
                self.holders[task] -= 1

    async def get(self, org: str, subject: str, kind: str, **_kw: Any):
        return self.rows.get((org, subject, kind))

    async def put(self, account: ConnectedAccount, **_kw: Any):
        key = account.org, account.subject, account.kind
        previous = self.rows.get(key)
        self.rows[key] = replace(account, version=previous.version + 1 if previous else 1)

    async def erase(self, org: str, subjects: list[str], *, kind: str, **_kw: Any):
        for key in list(self.rows):
            if key[0] == org and key[1] in subjects and key[2] == kind:
                del self.rows[key]

    async def query_one(self, query: str, params: dict[str, Any]):
        assert "SELECT count() AS n FROM loom.connected_accounts WHERE" in query
        assert "FINAL" not in query
        account = self.rows.get((params["org"], params["subject"], params["kind"]))
        if account is None:
            return {"n": 0}
        return {
            "n": int(
                not (
                    account.connection_id == params.get("keep")
                    and not account.active
                    and not account.account_id
                    and not account.workspace_id
                    and not account.account_email_enc
                    and not account.credential_enc
                    and not account.scopes
                    and not account.refresh_id
                    and account.connected_at is None
                )
            )
        }

    async def sources(self, _org: str, _subject: str):
        return []

    async def audit(self, event: str, **fields: Any):
        self.audits.append({"event": event, **fields})

    async def provider(self, request: httpx.Request) -> httpx.Response:
        assert not self.holders.get(asyncio.current_task(), 0)
        self.requests.append(request)
        if str(request.url) == "https://oauth2.googleapis.com/token":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["authorization_code"]
            assert form["redirect_uri"] == ["https://app.example/prefix/v1/connect/gdrive/callback"]
            code = form["code"][0]
            self.exchange_active += 1
            try:
                if self.pause_exchange and code == "older-code":
                    self.exchange_entered.set()
                    await self.exchange_release.wait()
                if self.http_failure:
                    return httpx.Response(503, stream=_UnreadErrorBody())
                return httpx.Response(
                    200,
                    json={
                        "access_token": _ACCESS + code,
                        "refresh_token": None if self.invalid_grant else _REFRESH + code,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": f"{_DRIVE} openid {_EMAIL}",
                    },
                )
            finally:
                self.exchange_active -= 1
        assert str(request.url) == "https://openidconnect.googleapis.com/v1/userinfo"
        return httpx.Response(
            200,
            json={"sub": "provider-person", "email": "display@example.com"},
        )

    def expire(self, state: str) -> None:
        key = "pending:connect:" + token_id(state)
        value, _expiry = self.keeper[key]
        self.keeper[key] = value, int(time.time()) - 1

    def account(self) -> ConnectedAccount:
        return self.rows["team", "person", "gdrive"]

    async def entered_exchange(self, work: asyncio.Future[Any]) -> None:
        entered = asyncio.create_task(self.exchange_entered.wait())
        try:
            await asyncio.wait((entered, work), return_when=asyncio.FIRST_COMPLETED)
            if not self.exchange_entered.is_set():
                work.result()
                raise AssertionError("callback ended before reaching the provider")
        finally:
            entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    async def provider(request: httpx.Request) -> httpx.Response:
        return await external.provider(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        external = _World(monkeypatch, http)
        yield external
        assert external.exchange_active == 0


class _SCIMWorld(_World):
    """Directory and SCIM rows behind the real connected-account standing check."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
        super().__init__(monkeypatch, http)
        self.contacts = ["person@example.com"]
        self.people: dict[str, dict[str, Any]] = {}

    def provision(self, email: str, *, active: bool = True, deleted: bool = False) -> None:
        self.people[accounts.scim.user_id(email)] = {
            "id": accounts.scim.user_id(email),
            "email": email,
            "active": active,
            "deleted": deleted,
            "work_addresses": set(),
        }

    async def query_all(self, query: str, params: Any = None, **kw: Any):
        if "FROM loom.scim_users" in query:
            return [self.people[uid] for uid in params["ids"] if uid in self.people]
        if "SELECT DISTINCT" in query and "AS owner" in query:
            assert params["org"] == "team"
            return (
                [{"owner": "person", "oversized": False}]
                if params["sub"] in {"person", *self.contacts}
                else []
            )
        rows = await super().query_all(query, params, **kw)
        return [
            dict(rows[0], namespace=f"personal-person-{index}", contact_email=email)
            for index, email in enumerate(self.contacts)
        ]

    async def query_one(self, query: str, params: dict[str, Any]):
        if "FROM loom.namespaces" in query and "account_count" in query:
            assert params["org"] == "team" and params["owner"] == "person"
            return {
                "account_count": len(self.contacts),
                "account_bytes": sum(len(email.encode()) for email in self.contacts),
                "accounts": [(email, True) for email in self.contacts],
            }
        if "FROM loom.scim_users" in query:
            if "missing" in params:
                qualified: set[str] = set()
                for uid in params["ids"]:
                    row = self.people.get(uid)
                    if row and row["active"] and not row["deleted"]:
                        if row["email"] in params["witnesses"]:
                            qualified.update(row["work_addresses"] & set(params["missing"]))
                return {"qualified": sorted(qualified)}
            row = self.people.get(params["id"])
            return {"x": 1} if row is not None and not row["active"] else None
        return await super().query_one(query, params)


@pytest.fixture
async def scim_world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_SCIMWorld]:
    async def provider(request: httpx.Request) -> httpx.Response:
        return await external.provider(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        external = _SCIMWorld(monkeypatch, http)
        monkeypatch.setattr(accounts.settings, "cell", "first")
        monkeypatch.setattr(accounts.settings, "scim_token", "lsc_first_" + "a" * 64)
        monkeypatch.setattr(cell, "mode", lambda: "enforced")
        monkeypatch.setattr(cell, "deployment_id", lambda: "connected-account-standing-test")
        monkeypatch.setattr(accounts.scim, "get_client", lambda: external)
        monkeypatch.setattr(accounts.scim, "deactivated", _SCIM_DEACTIVATED)
        yield external
        assert external.exchange_active == 0


@pytest.mark.parametrize(
    "scenario,allowed",
    [
        ("missing", False),
        ("active", True),
        ("inactive", False),
        ("deleted", False),
        ("scim_off", True),
        ("standalone", True),
        ("pre_enforcement", True),
        ("missing_alias", False),
        ("inactive_alias", False),
        ("revoked_session", False),
        ("inactive_scim_off", False),
        ("inactive_account", False),
    ],
)
@pytest.mark.asyncio
async def test_connected_consent_requires_the_complete_live_scim_person(
    scim_world: _SCIMWorld, monkeypatch: pytest.MonkeyPatch, scenario: str, allowed: bool
):
    if scenario in {
        "active",
        "missing_alias",
        "inactive_alias",
        "revoked_session",
        "inactive_account",
    }:
        scim_world.provision("person@example.com")
    elif scenario in {"inactive", "inactive_scim_off"}:
        scim_world.provision("person@example.com", active=False)
    elif scenario == "deleted":
        scim_world.provision("person@example.com", deleted=True)
    if scenario in {"scim_off", "inactive_scim_off"}:
        monkeypatch.setattr(accounts.settings, "scim_token", "")
    elif scenario == "standalone":
        monkeypatch.setattr(accounts.settings, "cell", "")
    elif scenario == "pre_enforcement":
        monkeypatch.setattr(cell, "mode", lambda: "recording")
    if scenario in {"missing_alias", "inactive_alias"}:
        scim_world.contacts.append("alias@example.com")
        if scenario == "inactive_alias":
            scim_world.provision("alias@example.com", active=False)
    if scenario == "inactive_account":
        scim_world.active = False
    if scenario == "revoked_session":
        scim_world.revocations["primary-session"] = None

    if allowed:
        _url, state, _binding = await accounts.start(_auth(), "gdrive")
        pending = await accounts.pending_store.pending("connect:" + token_id(state))
        assert pending is not None and pending["subject"] == "person"
        assert not scim_world.account().active
    else:
        with pytest.raises(accounts.OwnerUnavailable):
            await accounts.start(_auth(), "gdrive")
        assert scim_world.rows == scim_world.keeper == {}
    assert scim_world.directory_reads > 0
    assert scim_world.requests == []


@pytest.mark.asyncio
async def test_primary_start_keeps_browser_binding_and_refresh_material_separate(world: _World):
    url, state, binding = await accounts.start(_auth(), "gdrive")
    consent = parse_qs(urlsplit(url).query)
    assert consent["redirect_uri"] == ["https://app.example/prefix/v1/connect/gdrive/callback"]
    assert consent["state"] == [state]
    assert consent["code_challenge_method"] == ["S256"]
    pending = await accounts.pending_store.pending("connect:" + token_id(state))
    assert pending is not None
    assert pending["subject"] == "person" and pending["session_id"] == "primary-session"
    assert pending["binding"] == token_id(binding) and pending["binding"] != binding
    assert pending["expected_connection"] == world.account().connection_id
    assert not world.account().active and not world.requests

    response = await routes.callback("gdrive", _request("gdrive", state, binding))
    account = world.account()
    assert response.body == b'{"connected":true}'
    assert response.headers["cache-control"] == "no-store"
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert account.active and account.account_id == "provider-person"
    assert unseal(CREDENTIAL_PURPOSE, account.credential_enc) == _REFRESH + "older-code"
    rendered = json.dumps(
        {"pending": pending, "row": asdict(account), "audit": world.audits}, default=str
    )
    assert _ACCESS not in rendered and _REFRESH not in rendered
    assert "display@example.com" not in rendered


@pytest.mark.asyncio
async def test_signed_primary_route_cookie_matches_configured_callback_prefix(world: _World):
    response = await routes.start("gdrive", _start_request("Bearer " + _primary_token()))
    assert response.headers["cache-control"] == "no-store"
    url = json.loads(response.body)["authorization_url"]
    query = parse_qs(urlsplit(url).query)
    state = query["state"][0]
    redirect = query["redirect_uri"][0]
    assert redirect == "https://app.example/prefix/v1/connect/gdrive/callback"
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    cookie = cookies[routes._cookie(state)]
    assert cookie["path"] == urlsplit(redirect).path
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "lax"
    assert int(cookie["max-age"]) > 0
    jar = httpx.Cookies()
    jar.extract_cookies(
        httpx.Response(
            200,
            headers={"set-cookie": response.headers["set-cookie"]},
            request=httpx.Request("POST", "https://app.example/prefix/v1/connect/gdrive/start"),
        )
    )
    callback_request = httpx.Request("GET", redirect)
    jar.set_cookie_header(callback_request)
    assert callback_request.headers["cookie"] == f"{cookie.key}={cookie.value}"
    done = await routes.callback("gdrive", _request("gdrive", state, cookie.value))
    expired = SimpleCookie()
    expired.load(done.headers["set-cookie"])
    assert expired[cookie.key]["path"] == cookie["path"]
    assert expired[cookie.key]["max-age"] == "0"


@pytest.mark.parametrize(
    "accept,redirected",
    [
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
        ("application/json;q=0.5, text/html;q=0.9", True),
        ("text/*;q=0.8, application/json;q=0.4", True),
        ("application/json, text/html;q=0.8", False),
        ("application/json, text/html", False),
        ("*/*", False),
        ("", False),
        ("text/html;q=0, */*;q=1", False),
        ("text/html;q=invalid", False),
        ("text/html;q=nan", False),
        ("text/html;q=2", False),
    ],
)
@pytest.mark.asyncio
async def test_callback_negotiates_browser_return_without_consent_parameters(
    world: _World, accept: str, redirected: bool
):
    _url, state, binding = await accounts.start(_auth(), "gdrive")
    request = _request("gdrive", state, binding, accept=accept)
    request.scope["query_string"] += b"&return_to=https%3A%2F%2Funtrusted.example%2Fcollect"
    response = await routes.callback("gdrive", request)
    assert world.account().active
    assert response.status_code == (302 if redirected else 200)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["vary"] == "Accept"
    expired = SimpleCookie()
    expired.load(response.headers["set-cookie"])
    assert expired[routes._cookie(state)]["path"] == "/prefix/v1/connect/gdrive/callback"
    assert expired[routes._cookie(state)]["max-age"] == "0"
    if redirected:
        assert response.headers["location"] == "/settings/connected-accounts"
        assert response.body == b""
    else:
        assert "location" not in response.headers
        assert json.loads(response.body) == {"connected": True}
    public = str(dict(response.headers)) + str(response.body)
    for secret in (state, binding, "older-code", _ACCESS, _REFRESH, "untrusted.example"):
        assert secret not in public
    with pytest.raises(ValidationError):
        await routes.callback("gdrive", request)


@pytest.mark.asyncio
async def test_browser_callback_refuses_unbound_consent_before_redirecting(world: _World):
    _url, state, _binding = await accounts.start(_auth(), "gdrive")
    with pytest.raises(ValidationError):
        await routes.callback(
            "gdrive", _request("gdrive", state, "other-browser", accept="text/html")
        )
    assert not world.requests and not world.account().active


@pytest.mark.parametrize("credential", ["missing", "malformed", "on_behalf"])
@pytest.mark.asyncio
async def test_start_route_requires_its_own_valid_bearer(world: _World, credential: str):
    authorization = (
        None
        if credential == "missing"
        else "Bearer broken"
        if credential == "malformed"
        else "Bearer " + _primary_token()
    )
    error = ValidationError if credential == "on_behalf" else HTTPException
    with pytest.raises(error) as caught:
        await routes.start(
            "gdrive", _start_request(authorization, on_behalf=credential == "on_behalf")
        )
    if isinstance(caught.value, HTTPException):
        assert caught.value.status_code == 401
    assert world.rows == world.keeper == {}
    assert not world.requests and world.directory_reads == 0


@pytest.mark.parametrize(
    "changed",
    [
        {"session_id": ""},
        {"is_service_account": True},
        {"creator": "person", "parents": ("delegating-token",)},
    ],
)
@pytest.mark.asyncio
async def test_non_primary_identity_cannot_start_provider_consent(
    world: _World, changed: dict[str, Any]
):
    with pytest.raises(AuthInsufficientError):
        await accounts.start(_auth(**changed), "gdrive")
    assert world.rows == world.keeper == {}
    assert world.requests == [] and world.directory_reads == 0


@pytest.mark.asyncio
async def test_unconfigured_provider_refuses_before_directory_or_keeper_work(
    world: _World, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(accounts.settings, "connect_gdrive_client_secret", "")
    with pytest.raises(ValidationError, match="connected account is unavailable"):
        await accounts.start(_auth(), "gdrive")
    assert world.rows == world.keeper == {}
    assert world.requests == [] and world.directory_reads == 0


@pytest.mark.parametrize("revocation", ["outright", "cutoff"])
@pytest.mark.asyncio
async def test_revoked_primary_session_cannot_start_consent(world: _World, revocation: str):
    world.revocations["primary-session"] = (
        None if revocation == "outright" else int(time.time()) + 3600
    )
    with pytest.raises(ValidationError, match="connected account is unavailable"):
        await accounts.start(_auth(), "gdrive")
    assert world.rows == world.keeper == {}
    assert world.requests == []


@pytest.mark.parametrize("invalid", ["cookie", "provider", "expired"])
@pytest.mark.asyncio
async def test_unbound_or_expired_callback_never_exchanges_provider_code(
    world: _World, invalid: str
):
    _url, state, binding = await accounts.start(_auth(), "gdrive")
    kind = "linear" if invalid == "provider" else "gdrive"
    if invalid == "expired":
        world.expire(state)
    with pytest.raises(ValidationError):
        await routes.callback(
            kind, _request(kind, state, "another-browser" if invalid == "cookie" else binding)
        )
    assert not world.requests and not world.account().active
    pending = await accounts.pending_store.pending("connect:" + token_id(state))
    assert (pending is None) == (invalid == "expired")


@pytest.mark.asyncio
async def test_concurrent_and_replayed_callbacks_exchange_exactly_once(world: _World):
    _url, state, binding = await accounts.start(_auth(), "gdrive")
    world.pause_exchange = True
    tasks = [
        asyncio.create_task(accounts.callback("gdrive", state, binding, "older-code"))
        for _ in range(2)
    ]
    completed = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await world.entered_exchange(completed)
    finally:
        world.exchange_release.set()
        outcomes = await completed
    assert sum(value is None for value in outcomes) == 1
    assert sum(isinstance(value, ValidationError) for value in outcomes) == 1
    assert world.account().active
    assert len(world.requests) == 2
    assert await accounts.pending_store.pending("connect:" + token_id(state)) is None
    with pytest.raises(ValidationError):
        await accounts.callback("gdrive", state, binding, "older-code")
    assert len(world.requests) == 2


@pytest.mark.asyncio
async def test_standing_lost_during_exchange_prevents_connection_publication(world: _World):
    _url, state, binding = await accounts.start(_auth(), "gdrive")
    before = world.account()
    world.pause_exchange = True
    callback = asyncio.create_task(accounts.callback("gdrive", state, binding, "older-code"))
    try:
        await world.entered_exchange(callback)
        world.active = False
    finally:
        world.exchange_release.set()
        outcomes = await asyncio.gather(callback, return_exceptions=True)
    assert isinstance(outcomes[0], ValidationError)
    assert world.account() == before and not world.account().credential_enc
    assert world.audits == []
    assert await accounts.pending_store.pending("connect:" + token_id(state)) is None


@pytest.mark.parametrize("change", ["disconnect", "replacement"])
@pytest.mark.asyncio
async def test_late_callback_cannot_overwrite_a_stop_or_replacement(world: _World, change: str):
    _url, state, binding = await accounts.start(_auth(), "gdrive")
    original = world.account().connection_id
    world.pause_exchange = True
    callback = asyncio.create_task(accounts.callback("gdrive", state, binding, "older-code"))
    try:
        await world.entered_exchange(callback)
        if change == "disconnect":
            await accounts.disconnect("team", "person", "gdrive")
        else:
            _new_url, new_state, new_binding = await accounts.start(_auth(), "gdrive")
            await accounts.callback("gdrive", new_state, new_binding, "replacement-code")
        current = world.account()
        assert current.connection_id != original
        assert current.connection_id.partition("/")[2].startswith(
            "0" if change == "disconnect" else "1"
        )
    finally:
        world.exchange_release.set()
        outcomes = await asyncio.gather(callback, return_exceptions=True)
    assert isinstance(outcomes[0], ValidationError)
    assert world.account() == current
    assert world.account().active == (change == "replacement")
    assert await accounts.pending_store.pending("connect:" + token_id(state)) is None


@pytest.mark.asyncio
async def test_cancelled_exchange_consumes_callback_without_persisting_credentials(world: _World):
    _url, state, binding = await accounts.start(_auth(), "gdrive")
    before = world.account()
    world.pause_exchange = True
    callback = asyncio.create_task(accounts.callback("gdrive", state, binding, "older-code"))
    try:
        await world.entered_exchange(callback)
        callback.cancel()
        with pytest.raises(asyncio.CancelledError):
            await callback
    finally:
        callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)
    assert world.account() == before and not world.account().credential_enc
    assert world.exchange_active == 0
    assert await accounts.pending_store.pending("connect:" + token_id(state)) is None
    with pytest.raises(ValidationError):
        await accounts.callback("gdrive", state, binding, "older-code")
    assert len(world.requests) == 1


@pytest.mark.asyncio
async def test_provider_refusal_and_invalid_rotation_are_redacted_and_cannot_replay(world: _World):
    for attempt, failure in enumerate(("http", "rotation"), 1):
        _url, state, binding = await accounts.start(_auth(), "gdrive")
        before = world.account()
        world.http_failure = failure == "http"
        world.invalid_grant = failure == "rotation"
        with pytest.raises((provider_wire.OAuthError, ValidationError)) as caught:
            await accounts.callback("gdrive", state, binding, "older-code")
        if isinstance(caught.value, provider_wire.OAuthError):
            assert str(caught.value) == (
                "provider_refused" if failure == "http" else "invalid_response"
            )
        else:
            assert caught.value.code == "CONNECTION_UNAVAILABLE"
        rendered = "".join(traceback.format_exception(caught.value))
        assert _ACCESS not in rendered and _REFRESH not in rendered
        assert "document-client-secret" not in rendered
        assert world.account() == before and not world.account().active
        assert await accounts.pending_store.pending("connect:" + token_id(state)) is None
        with pytest.raises(ValidationError):
            await accounts.callback("gdrive", state, binding, "older-code")
        assert len(world.requests) == attempt
