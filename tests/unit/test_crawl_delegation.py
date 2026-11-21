"""Read admission requires each distinct write grant's explicit denial."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from server.clients import gdrive
from server.clients import google_workspace as google
from server.clients._vendor import CredentialRefused, TooLarge, VendorError
from server.knowledge.app import crawl_delegation
from tests.unit.test_google_workspace import _Body, _http
from tests.unit.test_google_workspace import account as account

pytestmark = pytest.mark.asyncio
_SUBJECT = "directory@example.com"


class _Standing:
    def __init__(self) -> None:
        self.live = True

    async def validate(self) -> None:
        if not self.live:
            raise PermissionError("source stopped")


def _claims(request: httpx.Request, account: Any) -> dict[str, Any]:
    assert str(request.url) == "https://oauth2.googleapis.com/token"
    assert not request.headers.get("authorization")
    form = parse_qs(request.content.decode())
    assert set(form) == {"grant_type", "assertion"}
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(
        form["assertion"][0], account[1], algorithms=["RS256"], audience=str(request.url)
    )
    assert set(claims) == {"iss", "sub", "scope", "aud", "iat", "exp"}
    assert claims["iss"] == account[0].client_email and claims["sub"] == _SUBJECT
    assert claims["exp"] - claims["iat"] == 600
    return claims


async def test_all_three_reads_and_four_distinct_write_denials_are_required(
    monkeypatch: pytest.MonkeyPatch, account: Any
) -> None:
    scopes: list[str] = []

    def response(request: httpx.Request) -> httpx.Response:
        scope = _claims(request, account)["scope"]
        scopes.append(scope)
        if scope in {read.value for read in gdrive.ReadScope}:
            return httpx.Response(
                200,
                json={"access_token": "private", "token_type": "Bearer", "expires_in": 60},
            )
        return httpx.Response(400, json={"error": "unauthorized_client"})

    async with _http(monkeypatch, response) as calls:
        await crawl_delegation.require_read_only(
            account[0],
            subject=_SUBJECT,
            standing=_Standing(),
            deadline=time.monotonic() + 10,
            max_bytes=4096,
        )
    assert len(calls) == 7
    assert scopes == [*(s.value for s in gdrive.ReadScope), *(s.value for s in gdrive.WriteScope)]
    assert gdrive.WriteScope.DIRECTORY_GROUPS.value != gdrive.WriteScope.DIRECTORY_MEMBERS.value


@pytest.mark.parametrize("scope", list(gdrive.WriteScope))
async def test_each_successful_write_grant_refuses_admission(
    monkeypatch: pytest.MonkeyPatch, account: Any, scope: gdrive.WriteScope
) -> None:
    def response(request: httpx.Request) -> httpx.Response:
        assert _claims(request, account)["scope"] == scope.value
        return httpx.Response(200, json={"access_token": "secret-write-token"})

    async with _http(monkeypatch, response) as calls:
        with pytest.raises(CredentialRefused) as error:
            await google.require_write_denied(
                account[0],
                subject=_SUBJECT,
                scope=scope,
                deadline=time.monotonic() + 10,
                max_bytes=4096,
            )
    assert len(calls) == 1 and "secret-write-token" not in str(error.value)


@pytest.mark.parametrize(
    "status,error",
    [
        (400, "invalid_grant"),
        (400, "access_denied"),
        (403, "unauthorized_client"),
        (429, "unauthorized_client"),
        (503, "unauthorized_client"),
        (302, "unauthorized_client"),
    ],
)
async def test_uncertain_or_non_scope_failure_is_never_read_only_proof(
    monkeypatch: pytest.MonkeyPatch, account: Any, status: int, error: str
) -> None:
    async with _http(
        monkeypatch,
        lambda _: httpx.Response(
            status,
            json={"error": error, "error_description": "secret-private-response"},
            headers={"location": "https://untrusted.example/redirect"},
        ),
    ) as calls:
        with pytest.raises(VendorError) as failure:
            await google.require_write_denied(
                account[0],
                subject=_SUBJECT,
                scope=gdrive.WriteScope.DIRECTORY_MEMBERS,
                deadline=time.monotonic() + 10,
                max_bytes=4096,
            )
    assert len(calls) == 1 and "secret-private-response" not in str(failure.value)


async def test_oversized_denial_closes_stream_without_accepting_it(
    monkeypatch: pytest.MonkeyPatch, account: Any
) -> None:
    body = _Body(b'{"error":"unauthorized_client",', b'"private":"' + b"s" * 100 + b'"}')
    async with _http(monkeypatch, lambda _: httpx.Response(400, stream=body)):
        with pytest.raises(TooLarge):
            await google.require_write_denied(
                account[0],
                subject=_SUBJECT,
                scope=gdrive.WriteScope.DRIVE,
                deadline=time.monotonic() + 10,
                max_bytes=32,
            )
    assert body.closed


async def test_standing_lost_on_a_denial_stops_before_the_next_scope(
    monkeypatch: pytest.MonkeyPatch, account: Any
) -> None:
    standing = _Standing()

    def response(request: httpx.Request) -> httpx.Response:
        scope = _claims(request, account)["scope"]
        if scope in {read.value for read in gdrive.ReadScope}:
            return httpx.Response(
                200,
                json={
                    "access_token": "private",
                    "token_type": "Bearer",
                    "expires_in": 60,
                },
            )
        standing.live = False
        return httpx.Response(400, json={"error": "unauthorized_client"})

    async with _http(monkeypatch, response) as calls:
        with pytest.raises(PermissionError):
            await crawl_delegation.require_read_only(
                account[0],
                subject=_SUBJECT,
                standing=standing,
                deadline=time.monotonic() + 10,
                max_bytes=4096,
            )
    assert len(calls) == 4
