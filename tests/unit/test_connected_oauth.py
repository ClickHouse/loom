"""Connected OAuth clients send provider-specific requests to fixed origins."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from server.clients import connected_oauth as oauth
from server.clients.notion import API_VERSION

_STATE = "browser_binding_0123456789abcdef0123456789abcdef"
# RFC 7636's S256 example, independently fixing the expected wire challenge.
_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_DRIVE = "https://www.googleapis.com/auth/drive.readonly"
_EMAIL = "https://www.googleapis.com/auth/userinfo.email"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    value = [100.0]
    monkeypatch.setattr(oauth, "_now", lambda: value[0])
    return value


def _client(kind: oauth.Kind) -> oauth.OAuthClient:
    return oauth.OAuthClient(kind, "client", "client-secret", "https://app.example/prefix/")


def _tokens(kind: oauth.Kind, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": "access-private",
        "refresh_token": "refresh-private",
        "token_type": "Bearer",
    }
    if kind != "notion":
        body.update(
            expires_in=3600, scope=f"{_DRIVE} openid {_EMAIL}" if kind == "gdrive" else "read"
        )
    return {**body, **extra}


@asynccontextmanager
async def _wire(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> AsyncIterator[list[httpx.Request]]:
    seen: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = handler(request)
        return await reply if inspect.isawaitable(reply) else reply

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True, trust_env=False
    ) as client:
        monkeypatch.setattr(oauth, "get_client", lambda: client)
        yield seen


def _form(request: httpx.Request) -> dict[str, list[str]]:
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    return parse_qs(request.content.decode())


@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear"])
def test_consent_has_fixed_provider_callback_state_and_supported_pkce(kind: oauth.Kind) -> None:
    client = _client(kind)
    url = urlsplit(
        client.authorization_url(_STATE, verifier=None if kind == "notion" else _VERIFIER)
    )
    expected = {
        "gdrive": ("accounts.google.com", "/o/oauth2/v2/auth"),
        "notion": ("api.notion.com", "/v1/oauth/authorize"),
        "linear": ("linear.app", "/oauth/authorize"),
    }
    assert url.scheme == "https" and (url.netloc, url.path) == expected[kind]
    query = parse_qs(url.query)
    assert query["client_id"] == ["client"] and query["state"] == [_STATE]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == [f"https://app.example/prefix/v1/connect/{kind}/callback"]
    assert "client-secret" not in url.geturl() and _VERIFIER not in url.geturl()
    if kind == "notion":
        assert query["owner"] == ["user"]
        assert not {"scope", "code_challenge", "access_type"}.intersection(query)
    else:
        assert query["code_challenge"] == [_CHALLENGE]
        assert query["code_challenge_method"] == ["S256"]
        assert set(query["scope"][0].split()) == (
            {_DRIVE, "openid", "email"} if kind == "gdrive" else {"read"}
        )
        if kind == "gdrive":
            assert query["access_type"] == ["offline"]
            assert query["include_granted_scopes"] == ["false"]
            assert query["prompt"] == ["consent"]
        else:
            assert query["actor"] == ["user"] and "access_type" not in query


def test_invalid_configuration_state_and_pkce_refuse_before_building_a_consent_url() -> None:
    for base in (
        "http://external.example",
        "https://user:password@app.example",
        "https://app.example:broken",
        "https://app.example/?redirect=https://other.example",
        "https://app.example/\r\nredirect",
    ):
        with pytest.raises(oauth.OAuthError, match="^invalid_public_base$"):
            oauth.OAuthClient("gdrive", "client", "secret", base)
    with pytest.raises(oauth.OAuthError, match="^not_configured$"):
        oauth.OAuthClient("gdrive", "client", "", "https://app.example")
    with pytest.raises(oauth.OAuthError, match="^invalid_state$"):
        _client("gdrive").authorization_url(
            "state&redirect_uri=https://other.example", verifier=_VERIFIER
        )
    with pytest.raises(oauth.OAuthError, match="^invalid_pkce$"):
        _client("gdrive").authorization_url(_STATE, verifier="short")
    with pytest.raises(oauth.OAuthError, match="^unsupported_pkce$"):
        _client("notion").authorization_url(_STATE, verifier=_VERIFIER)


@pytest.mark.asyncio
async def test_google_exchange_uses_form_pkce_canonical_scopes_and_stable_userinfo_subject(
    monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            assert request.method == "POST" and request.url.path == "/token"
            assert _form(request) == {
                "grant_type": ["authorization_code"],
                "code": ["opaque-code"],
                "redirect_uri": ["https://app.example/prefix/v1/connect/gdrive/callback"],
                "code_verifier": [_VERIFIER],
                "client_id": ["client"],
                "client_secret": ["client-secret"],
            }
            assert request.extensions["timeout"]["read"] == 30
            clock[0] = 112
            return httpx.Response(200, json=_tokens("gdrive"))
        assert str(request.url) == "https://openidconnect.googleapis.com/v1/userinfo"
        assert request.method == "GET" and not request.content
        assert request.headers["authorization"] == "Bearer access-private"
        assert request.extensions["timeout"]["read"] == 18
        return httpx.Response(200, json={"sub": "stable-sub", "email": "display@example.com"})

    async with _wire(monkeypatch, reply) as seen:
        grant = await _client("gdrive").exchange("opaque-code", verifier=_VERIFIER, deadline=130)
    assert len(seen) == 2
    assert grant.account_id == "stable-sub" and grant.display_email == "display@example.com"
    assert grant.workspace_id is None
    assert set(grant.tokens.scopes) == {_DRIVE, "openid", "email"}
    assert grant.tokens.expires_in == 3600 and grant.tokens.refresh_token == "refresh-private"
    assert "private" not in repr(grant) + repr(grant.tokens)
    assert "client-secret" not in repr(_client("gdrive"))


@pytest.mark.asyncio
async def test_notion_exchange_uses_basic_json_owner_id_and_optional_display_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.notion.com/v1/oauth/token"
        assert (
            request.headers["authorization"]
            == "Basic " + base64.b64encode(b"client:client-secret").decode()
        )
        assert request.headers["Notion-Version"] == API_VERSION
        assert json.loads(request.content) == {
            "grant_type": "authorization_code",
            "code": "opaque-code",
            "redirect_uri": "https://app.example/prefix/v1/connect/notion/callback",
        }
        return httpx.Response(
            200,
            json=_tokens(
                "notion",
                owner={"type": "user", "user": {"id": "human-id", "person": {}}},
                workspace_id="workspace-id",
                bot_id="authorization-bot",
                scope="opaque-capability-text",
            ),
        )

    async with _wire(monkeypatch, reply) as seen:
        grant = await _client("notion").exchange("opaque-code", deadline=130)
    assert len(seen) == 1
    assert grant.account_id == "human-id" and grant.account_id != "authorization-bot"
    assert grant.workspace_id == "workspace-id" and grant.display_email is None
    assert grant.tokens.expires_in is None and grant.tokens.scopes == ()


@pytest.mark.asyncio
async def test_linear_exchange_resolves_bearer_viewer_and_accepts_legacy_read_scope_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            assert str(request.url) == "https://api.linear.app/oauth/token"
            assert _form(request) == {
                "grant_type": ["authorization_code"],
                "code": ["opaque-code"],
                "redirect_uri": ["https://app.example/prefix/v1/connect/linear/callback"],
                "code_verifier": [_VERIFIER],
                "client_id": ["client"],
                "client_secret": ["client-secret"],
            }
            return httpx.Response(200, json=_tokens("linear", scope=["read"]))
        assert str(request.url) == "https://api.linear.app/graphql"
        assert request.headers["authorization"] == "Bearer access-private"
        assert json.loads(request.content) == {"query": "query { viewer { id name email } }"}
        return httpx.Response(
            200, json={"data": {"viewer": {"id": "viewer-id", "email": "display@example.com"}}}
        )

    async with _wire(monkeypatch, reply) as seen:
        grant = await _client("linear").exchange("opaque-code", verifier=_VERIFIER, deadline=130)
    assert len(seen) == 2 and grant.account_id == "viewer-id"
    assert grant.tokens.scopes == ("read",)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear"])
async def test_refresh_sends_provider_credentials_and_retains_or_rotates_only_documented_material(
    monkeypatch: pytest.MonkeyPatch, kind: oauth.Kind
) -> None:
    response = _tokens(kind, access_token="new-access", refresh_token="new-refresh")
    if kind == "gdrive":
        del response["refresh_token"]
        del response["scope"]

    def reply(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url)
            == {
                "gdrive": "https://oauth2.googleapis.com/token",
                "notion": "https://api.notion.com/v1/oauth/token",
                "linear": "https://api.linear.app/oauth/token",
            }[kind]
        )
        fields = {"grant_type": "refresh_token", "refresh_token": "old-refresh"}
        if kind == "notion":
            assert json.loads(request.content) == fields
            assert request.headers["authorization"].startswith("Basic ")
            assert request.headers["Notion-Version"] == API_VERSION
        else:
            assert _form(request) == {
                k: [v]
                for k, v in {
                    **fields,
                    "client_id": "client",
                    "client_secret": "client-secret",
                }.items()
            }
        return httpx.Response(200, json=response)

    scopes = (
        tuple(sorted({_DRIVE, "openid", "email"}))
        if kind == "gdrive"
        else ("read",)
        if kind == "linear"
        else ()
    )
    async with _wire(monkeypatch, reply) as seen:
        tokens = await _client(kind).refresh("old-refresh", scopes=scopes, deadline=130)
    assert len(seen) == 1 and tokens.access_token == "new-access"
    assert tokens.refresh_token == ("old-refresh" if kind == "gdrive" else "new-refresh")
    assert tokens.scopes == scopes and tokens.expires_in == (None if kind == "notion" else 3600)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notion", "linear"])
async def test_rotating_provider_refuses_missing_or_null_replacement_refresh_without_retry(
    monkeypatch: pytest.MonkeyPatch, kind: oauth.Kind
) -> None:
    for present in (False, True):
        response = _tokens(kind)
        if present:
            response["refresh_token"] = None
        else:
            del response["refresh_token"]
        async with _wire(
            monkeypatch, lambda _request, reply=response: httpx.Response(200, json=reply)
        ) as seen:
            with pytest.raises(oauth.OAuthError, match="^invalid_response$"):
                await _client(kind).refresh(
                    "old-refresh", scopes=("read",) if kind == "linear" else (), deadline=130
                )
        assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gdrive", "linear"])
async def test_exchange_refuses_expanded_scope_before_identity_lookup(
    monkeypatch: pytest.MonkeyPatch, kind: oauth.Kind
) -> None:
    response = _tokens(kind)
    response["scope"] += " write"
    async with _wire(monkeypatch, lambda _request: httpx.Response(200, json=response)) as seen:
        with pytest.raises(oauth.OAuthError, match="^scope_refused$"):
            await _client(kind).exchange("opaque-code", verifier=_VERIFIER, deadline=130)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["has whitespace", "nonascii-\u00e9"])
async def test_invalid_bearer_material_is_refused_before_identity_http(
    monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    response = _tokens("gdrive", access_token=access)
    async with _wire(monkeypatch, lambda _request: httpx.Response(200, json=response)) as seen:
        with pytest.raises(oauth.OAuthError, match="^invalid_response$"):
            await _client("gdrive").exchange("opaque-code", verifier=_VERIFIER, deadline=130)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear"])
async def test_revocation_uses_documented_token_body_and_no_notion_refresh_guess(
    monkeypatch: pytest.MonkeyPatch, kind: oauth.Kind
) -> None:
    async with _wire(monkeypatch, lambda _request: httpx.Response(200, content=b"")) as seen:
        client = _client(kind)
        if kind == "notion":
            with pytest.raises(oauth.OAuthError, match="^unsupported_refresh_revocation$"):
                await client.revoke("refresh-private", refresh=True, deadline=130)
            assert not seen
        await client.revoke("revoke-private", refresh=kind != "notion", deadline=130)
    (request,) = seen
    assert (
        str(request.url)
        == {
            "gdrive": "https://oauth2.googleapis.com/revoke",
            "notion": "https://api.notion.com/v1/oauth/revoke",
            "linear": "https://api.linear.app/oauth/revoke",
        }[kind]
    )
    if kind == "notion":
        assert json.loads(request.content) == {"token": "revoke-private"}
        assert request.headers["authorization"].startswith("Basic ")
    else:
        assert _form(request) == (
            {"token": ["revoke-private"], "token_type_hint": ["refresh_token"]}
            if kind == "linear"
            else {"token": ["revoke-private"]}
        )


@pytest.mark.asyncio
async def test_provider_redirect_never_forwards_credentials_even_when_http_client_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                307, headers={"Location": "https://other.example/credential-sink"}
            )
        return httpx.Response(200, json=_tokens("gdrive"))

    async with _wire(monkeypatch, reply) as seen:
        with pytest.raises(oauth.OAuthError, match="^provider_refused$"):
            await _client("gdrive").refresh(
                "refresh-private", scopes=tuple(sorted({_DRIVE, "openid", "email"})), deadline=130
            )
    assert len(seen) == 1 and seen[0].url.host == "oauth2.googleapis.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "network"])
async def test_provider_failures_are_redacted_and_never_retried(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class ErrorBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise AssertionError("an error body must not be read")
            yield b""

    def reply(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError(
                "access-private refresh-private https://other.example/private", request=request
            )
        return httpx.Response(503, stream=ErrorBody())

    refresh = "refresh-private"
    async with _wire(monkeypatch, reply) as seen:
        with pytest.raises(oauth.OAuthError) as caught:
            await _client("linear").refresh(refresh, scopes=("read",), deadline=130)
    assert len(seen) == 1
    assert str(caught.value) == ("provider_refused" if failure == "http" else "unavailable")
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    for secret in (
        "access-private",
        "refresh-private",
        "client-secret",
        "https://other.example/private",
    ):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_stream_body_stops_at_the_metadata_cap_and_closes_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Body(httpx.AsyncByteStream):
        pieces = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"x" * 32768, b"x" * 32768, b"x", b"tail-must-not-be-read"):
                self.pieces += 1
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    body = Body()
    async with _wire(monkeypatch, lambda _request: httpx.Response(200, stream=body)) as seen:
        with pytest.raises(oauth.OAuthError, match="^response_too_large$"):
            await _client("linear").refresh("refresh-private", scopes=("read",), deadline=130)
    assert len(seen) == 1 and body.pieces == 3 and body.closed


@pytest.mark.asyncio
async def test_expired_or_nonfinite_deadline_sends_no_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _wire(monkeypatch, lambda _request: httpx.Response(200, json={})) as seen:
        for deadline in (100, float("nan"), float("inf")):
            with pytest.raises(oauth.OAuthError, match="^deadline$"):
                await _client("linear").refresh(
                    "refresh-private", scopes=("read",), deadline=deadline
                )
    assert not seen


@pytest.mark.asyncio
async def test_body_read_cancellation_propagates_and_closes_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, never = asyncio.Event(), asyncio.Event()

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await never.wait()
            yield b"{}"

        async def aclose(self) -> None:
            self.closed = True

    body = Body()
    async with _wire(monkeypatch, lambda _request: httpx.Response(200, stream=body)) as seen:
        call = asyncio.create_task(
            _client("linear").refresh("refresh-private", scopes=("read",), deadline=130)
        )
        try:
            await entered.wait()
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
    assert len(seen) == 1 and body.closed


@pytest.mark.asyncio
async def test_operator_response_bound_limits_stream_without_reading_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth.settings, "knowledge_connected_oauth_response_max_bytes", 1024)

    class Body(httpx.AsyncByteStream):
        pieces = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in (b"x" * 1024, b"x", b"unread-tail"):
                self.pieces += 1
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    body = Body()
    async with _wire(monkeypatch, lambda _request: httpx.Response(200, stream=body)) as seen:
        with pytest.raises(oauth.OAuthError, match="^response_too_large$"):
            await _client("linear").refresh("refresh-private", scopes=("read",), deadline=130)
    assert len(seen) == 1
    assert body.pieces == 2 and body.closed


@pytest.mark.asyncio
async def test_request_timeout_obeys_operator_bound_and_shorter_caller_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oauth.settings, "knowledge_connected_oauth_timeout_s", 2.5)
    async with _wire(
        monkeypatch, lambda _request: httpx.Response(200, json=_tokens("linear"))
    ) as seen:
        await _client("linear").refresh("refresh-private", scopes=("read",), deadline=130)
        await _client("linear").refresh("refresh-private", scopes=("read",), deadline=101)
    assert len(seen) == 2
    assert all(value == 2.5 for value in seen[0].extensions["timeout"].values())
    assert all(value == 1.0 for value in seen[1].extensions["timeout"].values())
