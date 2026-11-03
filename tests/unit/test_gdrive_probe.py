"""The shared-folder probe over a mock transport: the token exchange, the folder
check, the listing, and how each failure is told apart. No error carries the
store's response body."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from server.clients import gdrive
from server.knowledge.app import source_probes

FOLDER = "1AbCdEfGhIjKlMnOp"
OTHER = "2QrStUvWxYzAbCdEf"
CONFIG = {"folder_ids": [FOLDER, OTHER], "include": ["slides", "pdf"]}


def _key() -> tuple[str, Any]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    credential = json.dumps(
        {"type": "service_account", "client_email": "sync@example.com", "private_key": pem}
    )
    return credential, private.public_key()


CREDENTIAL, PUBLIC = _key()


def _install(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_record))
    monkeypatch.setattr(gdrive, "get_client", lambda: client)
    return seen


def _store(request: httpx.Request) -> httpx.Response:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "tok"})
    if request.url.path.endswith((f"/files/{FOLDER}", f"/files/{OTHER}")):
        return httpx.Response(200, json={"mimeType": "application/vnd.google-apps.folder"})
    # The store answers in the order the listing asks for: newest first.
    files = [
        {"id": "b", "name": "Newer", "modifiedTime": "2026-02-01T00:00:00Z"},
        {"id": "a", "name": "Older", "modifiedTime": "2026-01-01T00:00:00Z"},
    ]
    return httpx.Response(200, json={"files": files})


def test_the_probe_lists_the_folder_with_a_signed_assertion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install(monkeypatch, _store)
    found = asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))
    assert (found.documents, found.sample, found.more) == (2, ["Newer", "Older"], False)
    token_call, *folder_calls, list_call = seen
    assert len(folder_calls) == 2
    form = dict(httpx.QueryParams(token_call.content.decode()))
    claims = jwt.decode(
        form["assertion"],
        PUBLIC,
        algorithms=["RS256"],
        audience="https://oauth2.googleapis.com/token",
    )
    assert claims["iss"] == "sync@example.com"
    assert claims["scope"].endswith("/drive.readonly")
    assert all(c.headers["Authorization"] == "Bearer tok" for c in folder_calls)
    # One listing covers every folder.
    q = list_call.url.params["q"]
    assert f"('{FOLDER}' in parents or '{OTHER}' in parents) and trashed = false" in q
    assert list_call.url.params["orderBy"] == "modifiedTime desc"
    assert "application/pdf" in q and "presentation" in q and "document'" not in q


def test_a_key_that_is_not_a_service_account_is_refused_without_a_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install(monkeypatch, _store)
    for bad in ("not json", '{"type": "user"}', '{"type": "service_account"}'):
        with pytest.raises(source_probes.CredentialRefused):
            asyncio.run(source_probes.probe("gdrive", CONFIG, bad))
    assert seen == []


@pytest.mark.parametrize(
    ("where", "status", "error"),
    [
        ("token", 400, source_probes.CredentialRefused),
        ("folder", 403, source_probes.CredentialRefused),
        ("folder", 404, source_probes.SourceUnreachable),
        ("list", 429, source_probes.SourceUnreachable),
        ("list", 500, source_probes.SourceUnreachable),
    ],
)
def test_failures_are_told_apart_and_carry_no_body(
    monkeypatch: pytest.MonkeyPatch, where: str, status: int, error: type[Exception]
) -> None:
    body = "secret-title-from-the-store"

    def _handler(request: httpx.Request) -> httpx.Response:
        token = request.url.host == "oauth2.googleapis.com"
        folder = request.url.path.endswith(f"/files/{FOLDER}")
        if (where, token, folder) in {("token", True, False), ("folder", False, True)}:
            return httpx.Response(status, text=body)
        if where == "list" and not token and not folder:
            return httpx.Response(status, text=body)
        return _store(request)

    _install(monkeypatch, _handler)
    with pytest.raises(error) as e:
        asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))
    assert body not in str(e.value)


def test_an_id_that_is_not_a_folder_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/files/{FOLDER}"):
            return httpx.Response(200, json={"id": FOLDER, "mimeType": "application/pdf"})
        return _store(request)

    _install(monkeypatch, _handler)
    with pytest.raises(source_probes.SourceUnreachable):
        asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))


@pytest.mark.parametrize(
    "reason", ["userRateLimitExceeded", "dailyLimitExceeded", "rateLimitExceeded"]
)
def test_a_403_that_is_a_rate_limit_is_told_from_a_refusal(reason: str) -> None:
    body = {"error": {"code": 403, "errors": [{"reason": reason}], "message": "x"}}
    with pytest.raises(gdrive.RateLimited, match="rate_limited"):
        gdrive._raise_for(httpx.Response(403, json=body), "listing")
    status = {"error": {"code": 403, "status": "RESOURCE_EXHAUSTED"}}
    with pytest.raises(gdrive.RateLimited):
        gdrive._raise_for(httpx.Response(403, json=status), "listing")
    refused = {"error": {"code": 403, "errors": [{"reason": "insufficientFilePermissions"}]}}
    with pytest.raises(gdrive.CredentialRefused):
        gdrive._raise_for(httpx.Response(403, json=refused), "listing")
    with pytest.raises(gdrive.CredentialRefused):
        gdrive._raise_for(httpx.Response(403, text="not json"), "listing")


def test_a_rate_limited_probe_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            body = {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}
            return httpx.Response(403, json=body)
        return _store(request)

    _install(monkeypatch, _handler)
    with pytest.raises(source_probes.SourceUnreachable, match="rate_limited"):
        asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))


def test_a_probe_past_its_time_limit_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    async def _slow(config: dict[str, Any], credential: str) -> source_probes.Probe:
        await asyncio.sleep(5)
        return source_probes.Probe(documents=0)

    monkeypatch.setattr(settings, "knowledge_source_probe_timeout_s", 0.05)
    monkeypatch.setitem(source_probes.PROBES, "gdrive", _slow)
    with pytest.raises(source_probes.SourceUnreachable, match="within 0.05 s"):
        asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))


def test_a_folder_the_account_cannot_see_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/files/{OTHER}"):
            return httpx.Response(404, json={"error": {"code": 404}})
        return _store(request)

    seen = _install(monkeypatch, _handler)
    with pytest.raises(source_probes.SourceUnreachable) as e:
        asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))
    assert f"folder {OTHER}" in str(e.value) and FOLDER not in str(e.value)
    assert "share the folder with the service account" in str(e.value)
    assert not any(r.url.path.endswith("/files") for r in seen)  # no listing after it


@pytest.mark.parametrize(
    ("status", "body", "error", "text"),
    [
        (
            403,
            {"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
            source_probes.SourceUnreachable,
            "token exchange: rate_limited",
        ),
        (429, {}, source_probes.SourceUnreachable, "token exchange: rate_limited"),
        (
            403,
            {"error": "rate_limit_exceeded"},
            source_probes.SourceUnreachable,
            "token exchange: rate_limited",
        ),
        (
            400,
            {"error": "invalid_grant"},
            source_probes.CredentialRefused,
            "token exchange: http_400",
        ),
        (
            403,
            {"error": "access_denied"},
            source_probes.CredentialRefused,
            "token exchange: http_403",
        ),
    ],
)
def test_the_token_exchange_tells_a_rate_limit_from_a_refused_key(
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any], error: type, text: str
) -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(status, json=body)
        return _store(request)

    _install(monkeypatch, _handler)
    with pytest.raises(error, match=text):
        asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))


def _key_json(pem: str) -> str:
    return json.dumps(
        {"type": "service_account", "client_email": "sync@example.com", "private_key": pem}
    )


@pytest.mark.parametrize("which", ["ec", "public"])
def test_a_key_that_is_no_rsa_private_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    if which == "ec":
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
    else:
        pem = PUBLIC.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
    seen = _install(monkeypatch, _store)
    with pytest.raises(source_probes.CredentialRefused, match="not an RSA key"):
        asyncio.run(source_probes.probe("gdrive", CONFIG, _key_json(pem)))
    assert seen == []  # refused before any call


def test_a_failed_folder_check_cancels_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    finished: list[str] = []

    async def _require_folder(token: str, folder_id: str) -> None:
        started.append(folder_id)
        if folder_id == FOLDER:
            raise gdrive.NotFound(f"folder {folder_id}: http_404")
        await asyncio.sleep(5)
        finished.append(folder_id)

    async def _token(account: Any) -> str:
        return "tok"

    monkeypatch.setattr(gdrive, "access_token", _token)
    monkeypatch.setattr(gdrive, "require_folder", _require_folder)
    with pytest.raises(source_probes.SourceUnreachable, match=f"folder {FOLDER}"):
        asyncio.run(source_probes.probe("gdrive", CONFIG, CREDENTIAL))
    assert sorted(started) == sorted(CONFIG["folder_ids"]) and finished == []
