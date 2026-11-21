"""Delegated JWTs and bounded Directory HTTP; no registration or provider is needed."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from server.clients import gdrive
from server.clients import google_workspace as google
from server.clients._vendor import CredentialRefused, RateLimited, TooLarge, VendorError
from server.knowledge.app import crawl_authority
from server.settings import settings

pytestmark = pytest.mark.asyncio

_SUBJECT = "directory-admin@example.com"
_ACCESS = "private-delegated-access"


@pytest.fixture(scope="module")
def account() -> tuple[gdrive.ServiceAccount, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    credential = json.dumps(
        {
            "type": "service_account",
            "client_email": "service@example.com",
            "private_key": pem,
            "token_uri": "https://untrusted.example/token",
        }
    )
    return gdrive.parse_service_account(credential), private.public_key()


@asynccontextmanager
async def _http(monkeypatch: pytest.MonkeyPatch, handler: Any):
    calls: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    async def pace(delay: float) -> None:
        assert delay == gdrive.PAGE_PACE_S

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(record), follow_redirects=True
    ) as client:
        monkeypatch.setattr(google, "get_client", lambda: client)
        monkeypatch.setattr(google, "_pace", pace)
        monkeypatch.setattr(gdrive, "get_client", lambda: client)
        yield calls


def _token(scope: gdrive.ReadScope, *, deadline: float | None = None) -> google.DelegatedToken:
    return google.DelegatedToken(_ACCESS, _SUBJECT, scope, deadline or time.monotonic() + 30)


def _person(**changes: Any) -> dict[str, Any]:
    return {
        "id": "123456",
        "primaryEmail": "person@example.com",
        "aliases": ["alias@example.com"],
        "customerId": "customer-one",
        "suspended": False,
        "archived": False,
        **changes,
    }


class _Body(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _WaitingBody(_Body):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        await asyncio.Event().wait()
        yield b""


@pytest.mark.parametrize("scope", list(gdrive.ReadScope))
async def test_grant_signs_exact_delegated_subject_scope_and_fixed_endpoint(
    monkeypatch: pytest.MonkeyPatch, account: Any, scope: gdrive.ReadScope
) -> None:
    service, public = account

    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        form = parse_qs(request.content.decode())
        assert set(form) == {"grant_type", "assertion"}
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        claims = jwt.decode(
            form["assertion"][0], public, algorithms=["RS256"], audience=str(request.url)
        )
        assert set(claims) == {"iss", "sub", "scope", "aud", "iat", "exp"}
        assert (claims["iss"], claims["sub"], claims["scope"]) == (
            service.client_email,
            _SUBJECT,
            scope.value,
        )
        assert claims["exp"] - claims["iat"] == 600
        return httpx.Response(
            200,
            json={
                "access_token": _ACCESS,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": scope.value,
            },
        )

    clock = time.monotonic()

    def advancing() -> float:
        nonlocal clock
        clock += 0.001
        return clock

    monkeypatch.setattr(google, "_now", advancing)
    deadline = clock + 30
    async with _http(monkeypatch, answer) as calls:
        token = await google.delegated_token(
            service, subject=_SUBJECT, scope=scope, deadline=deadline, max_bytes=4096
        )
    assert len(calls) == 1 and token.deadline == deadline
    assert (token.subject, token.scope) == (_SUBJECT, scope)
    assert _ACCESS not in repr(token) and _SUBJECT not in repr(token)
    with pytest.raises(TypeError):
        str(token)
    with pytest.raises(TypeError):
        copy.copy(token)


async def test_shared_grant_keeps_its_original_payload_and_response_contract(
    monkeypatch: pytest.MonkeyPatch, account: Any
) -> None:
    service, public = account

    def answer(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        claims = jwt.decode(
            form["assertion"][0],
            public,
            algorithms=["RS256"],
            audience="https://oauth2.googleapis.com/token",
        )
        assert "sub" not in claims and claims["scope"] == gdrive.ReadScope.DRIVE.value
        return httpx.Response(200, json={"access_token": "ordinary-token"})

    async with _http(monkeypatch, answer) as calls:
        assert await gdrive.access_token(service) == "ordinary-token"
    assert len(calls) == 1 and calls[0].url.host == "oauth2.googleapis.com"


@pytest.mark.parametrize(
    ("subject", "scope"),
    [
        (None, gdrive.ReadScope.DRIVE),
        ("not-an-email", gdrive.ReadScope.DRIVE),
        ("person@example.com\n", gdrive.ReadScope.DIRECTORY_USERS),
        (_SUBJECT, "https://www.googleapis.com/auth/drive"),
        (_SUBJECT, "https://www.googleapis.com/auth/admin.directory.group.member"),
    ],
)
async def test_subject_and_scope_refusals_make_no_http_request(
    monkeypatch: pytest.MonkeyPatch, account: Any, subject: Any, scope: Any
) -> None:
    async with _http(monkeypatch, lambda request: pytest.fail("no request permitted")) as calls:
        with pytest.raises(CredentialRefused):
            await google.delegated_token(
                account[0],
                subject=subject,
                scope=scope,
                deadline=time.monotonic() + 30,
                max_bytes=4096,
            )
    assert calls == []


@pytest.mark.parametrize("status", [302, 400, 403, 429])
async def test_grant_refusals_do_not_read_echoed_secrets_follow_or_retry(
    monkeypatch: pytest.MonkeyPatch, account: Any, status: int
) -> None:
    body = _Body(b"private-provider-body")
    async with _http(
        monkeypatch,
        lambda request: httpx.Response(
            status, headers={"Location": "https://untrusted.example/private"}, stream=body
        ),
    ) as calls:
        with pytest.raises(VendorError) as caught:
            await google.delegated_token(
                account[0],
                subject=_SUBJECT,
                scope=gdrive.ReadScope.DRIVE,
                deadline=time.monotonic() + 30,
                max_bytes=4096,
            )
    assert caught.value.status == status and body.closed and body.reads == 0
    assert len(calls) == 1
    assert all(secret not in str(caught.value) for secret in ("private", _SUBJECT, "untrusted"))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"errors": [{"reason": "userRateLimitExceeded"}]}, RateLimited),
        ({"errors": [{"reason": "quotaExceeded"}]}, RateLimited),
        ({"status": "RESOURCE_EXHAUSTED"}, RateLimited),
        ({"errors": [{"reason": "forbidden"}]}, CredentialRefused),
    ],
)
async def test_directory_forbidden_distinguishes_quota_from_credentials_without_error_echo(
    monkeypatch: pytest.MonkeyPatch, error: dict[str, Any], expected: type[VendorError]
) -> None:
    body = _Body(json.dumps({"error": {**error, "message": "private-provider-body"}}).encode())
    async with _http(monkeypatch, lambda request: httpx.Response(403, stream=body)) as calls:
        with pytest.raises(expected) as caught:
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=4096,
            )
    assert type(caught.value) is expected and caught.value.status == 403
    assert len(calls) == 1 and body.reads == 1 and body.closed
    assert all(value not in str(caught.value) for value in ("private", _ACCESS, _SUBJECT))


@pytest.mark.parametrize("raw", [b"not-json", b'{"error":{"errors":12}}', b'{"error":{}}'])
async def test_ambiguous_directory_forbidden_does_not_claim_credential_revocation(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    body = _Body(raw)
    async with _http(monkeypatch, lambda request: httpx.Response(403, stream=body)) as calls:
        with pytest.raises(VendorError) as caught:
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=4096,
            )
    assert type(caught.value) is VendorError and caught.value.status == 403
    assert len(calls) == 1 and body.reads == 1 and body.closed
    assert raw.decode() not in str(caught.value)


async def test_directory_error_body_bound_stops_reading_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _Body(b"x" * 8, b"x" * 9, b"private-must-not-be-read")
    async with _http(monkeypatch, lambda request: httpx.Response(403, stream=body)) as calls:
        with pytest.raises(TooLarge) as caught:
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=16,
            )
    assert caught.value.status == 403 and "private" not in str(caught.value)
    assert len(calls) == 1 and body.reads == 2 and body.closed


@pytest.mark.parametrize(
    "change",
    [
        {"token_type": None},
        {"expires_in": True},
        {"access_token": "private\naccess"},
        {"scope": "https://www.googleapis.com/auth/drive"},
    ],
)
async def test_bad_token_metadata_never_returns_a_checkout(
    monkeypatch: pytest.MonkeyPatch, account: Any, change: dict[str, Any]
) -> None:
    answer = {"access_token": _ACCESS, "token_type": "Bearer", "expires_in": 3600, **change}
    async with _http(monkeypatch, lambda request: httpx.Response(200, json=answer)) as calls:
        with pytest.raises(VendorError):
            await google.delegated_token(
                account[0],
                subject=_SUBJECT,
                scope=gdrive.ReadScope.DRIVE,
                deadline=time.monotonic() + 30,
                max_bytes=4096,
            )
    assert len(calls) == 1


async def test_user_and_directory_identity_require_explicit_active_customer_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _token(gdrive.ReadScope.DIRECTORY_USERS)

    def answer(request: httpx.Request) -> httpx.Response:
        person = (
            _person()
            if request.url.path.endswith("/123456")
            else _person(id="directory-admin-id", primaryEmail=_SUBJECT, aliases=[])
        )
        return httpx.Response(200, json=person)

    async with _http(monkeypatch, answer) as calls:
        principal = await google.directory_identity(token, max_bytes=4096)
        person = await google.user(
            token, "123456", customer_id=principal.customer_id, max_bytes=4096
        )
    assert person.id == "123456" and person.email == "person@example.com"
    assert person.aliases == ("alias@example.com",)
    assert len(calls) == 2
    assert [r.url.path for r in calls] == [
        "/admin/directory/v1/users/" + _SUBJECT,
        "/admin/directory/v1/users/123456",
    ]
    assert all(r.url.host == "admin.googleapis.com" for r in calls)
    assert all(r.headers["Authorization"] == "Bearer " + _ACCESS for r in calls)
    assert all("deletionTime" in r.url.params["fields"] for r in calls)


@pytest.mark.parametrize(
    "change",
    [{"suspended": True}, {"archived": True}, {"deletionTime": "deleted"}, {"suspended": None}],
)
async def test_user_refuses_inactive_or_unproven_state(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    async with _http(monkeypatch, lambda request: httpx.Response(200, json=_person(**change))):
        with pytest.raises(CredentialRefused):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=4096,
            )


async def test_external_customer_is_not_an_admitted_employee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _http(
        monkeypatch,
        lambda request: httpx.Response(200, json=_person(customerId="external-customer")),
    ) as calls:
        with pytest.raises(CredentialRefused):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=4096,
            )
    assert len(calls) == 1


async def test_user_response_must_match_the_requested_id_or_verified_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _http(monkeypatch, lambda request: httpx.Response(200, json=_person())) as calls:
        with pytest.raises(VendorError, match="identity mismatch"):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "different-id",
                customer_id="customer-one",
                max_bytes=4096,
            )
    assert len(calls) == 1


async def test_members_pages_preserve_types_without_inferring_employee_status_or_expanding_customer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "admin.googleapis.com"
        assert request.url.path == "/admin/directory/v1/groups/group@example.com/members"
        assert request.url.params["maxResults"] == "200"
        assert request.url.params["includeDerivedMembership"] == "false"
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "members": [{"id": "external", "type": "USER", "email": "other@external.com"}],
                    "nextPageToken": "next-page",
                },
            )
        assert request.url.params["pageToken"] == "next-page"
        return httpx.Response(
            200,
            json={
                "members": [
                    {"id": "nested", "type": "GROUP", "email": "nested@example.com"},
                    {"id": "customer-one", "type": "CUSTOMER", "status": "UNDOCUMENTED"},
                ]
            },
        )

    async with _http(monkeypatch, page) as calls:
        members = await google.group_members(
            _token(gdrive.ReadScope.DIRECTORY_MEMBERS),
            "group@example.com",
            max_bytes=4096,
            max_pages=2,
        )
    assert [(m.kind, m.id, m.email) for m in members] == [
        ("USER", "external", "other@external.com"),
        ("GROUP", "nested", "nested@example.com"),
        ("CUSTOMER", "customer-one", None),
    ]
    assert len(calls) == 2 and all("/groups/" in r.url.path for r in calls)


@pytest.mark.parametrize("failure", ["repeated-token", "repeated-member", "page-bound", "bad-type"])
async def test_incomplete_membership_never_returns_a_partial_list(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    pages = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        pages += 1
        kind = "UNKNOWN" if failure == "bad-type" else "USER"
        identifier = str(pages) if failure != "repeated-member" else "same"
        return httpx.Response(
            200, json={"members": [{"type": kind, "id": identifier}], "nextPageToken": "same-page"}
        )

    async with _http(monkeypatch, answer) as calls:
        with pytest.raises(VendorError):
            await google.group_members(
                _token(gdrive.ReadScope.DIRECTORY_MEMBERS),
                "group@example.com",
                max_bytes=4096,
                max_pages=1 if failure == "page-bound" else 3,
            )
    assert len(calls) == (1 if failure in {"page-bound", "bad-type"} else 2)


async def test_membership_over_the_crawl_bound_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal pages
        pages += 1
        return httpx.Response(
            200,
            json={
                "members": [{"type": "USER", "id": f"{pages}-{n}"} for n in range(200)],
                "nextPageToken": str(pages),
            },
        )

    async with _http(monkeypatch, answer) as calls:
        with pytest.raises(TooLarge, match="crawl bound"):
            await google.group_members(
                _token(gdrive.ReadScope.DIRECTORY_MEMBERS),
                "group@example.com",
                max_bytes=32768,
                max_pages=11,
            )
    assert len(calls) == 11


@pytest.mark.parametrize("configured", [False, True])
async def test_response_cap_stops_reading_and_closes_stream(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    cap = 16
    if configured:
        monkeypatch.setattr(settings, "knowledge_crawl_directory_max_response_bytes", 1024)
        cap = crawl_authority.configured_bounds().directory.body_bytes
    body = _Body(b"x" * (cap // 2), b"x" * (cap // 2 + 1), b"must-not-be-read")
    async with _http(monkeypatch, lambda request: httpx.Response(200, stream=body)) as calls:
        with pytest.raises(TooLarge):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=cap,
            )
    assert len(calls) == 1 and body.reads == 2 and body.closed


async def test_elapsed_pacing_refuses_the_next_page_before_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = time.monotonic() + 30
    waits = []

    async def spent(delay: float) -> None:
        waits.append(delay)
        monkeypatch.setattr(google, "_now", lambda: deadline + 1)

    async with _http(
        monkeypatch, lambda request: httpx.Response(200, json={"nextPageToken": "next"})
    ) as calls:
        monkeypatch.setattr(google, "_pace", spent)
        with pytest.raises(TimeoutError):
            await google.group_members(
                _token(gdrive.ReadScope.DIRECTORY_MEMBERS, deadline=deadline),
                "group@example.com",
                max_bytes=4096,
                max_pages=2,
            )
    assert len(calls) == 1 and waits == [gdrive.PAGE_PACE_S]


async def test_invalid_bounds_scope_and_path_make_no_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _http(monkeypatch, lambda request: pytest.fail("no request permitted")) as calls:
        with pytest.raises(ValueError):
            await google.group_members(
                _token(gdrive.ReadScope.DIRECTORY_MEMBERS), "group", max_bytes=4096, max_pages=0
            )
        with pytest.raises(ValueError):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=True,
            )
        with pytest.raises(ValueError):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "../users/other",
                customer_id="customer-one",
                max_bytes=4096,
            )
        with pytest.raises(CredentialRefused):
            await google.user(
                _token(gdrive.ReadScope.DRIVE), "123456", customer_id="customer-one", max_bytes=4096
            )
        with pytest.raises(TimeoutError):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS, deadline=float("nan")),
                "123456",
                customer_id="customer-one",
                max_bytes=4096,
            )
    assert calls == []


@pytest.mark.parametrize("status", [200, 403])
async def test_cancellation_closes_the_owned_stream_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    body = _WaitingBody()
    async with _http(monkeypatch, lambda request: httpx.Response(status, stream=body)) as calls:
        task = asyncio.create_task(
            google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS),
                "123456",
                customer_id="customer-one",
                max_bytes=4096,
            )
        )
        await body.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) == 1 and body.closed


@pytest.mark.parametrize("status", [200, 403])
async def test_absolute_deadline_covers_stream_body_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    body = _WaitingBody()
    async with _http(monkeypatch, lambda request: httpx.Response(status, stream=body)) as calls:
        with pytest.raises(TimeoutError):
            await google.user(
                _token(gdrive.ReadScope.DIRECTORY_USERS, deadline=time.monotonic() + 0.1),
                "123456",
                customer_id="customer-one",
                max_bytes=4096,
            )
    assert len(calls) == 1 and body.closed
