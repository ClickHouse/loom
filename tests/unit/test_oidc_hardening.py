"""OIDC login trusts only what the configured IdP vouches for.

The discovery document must name the configured issuer, `email_verified`
must really be true, the JWKS client is reused (off the event loop), and a
failed callback never echoes the IdP's response to the browser.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from server.auth import oauth, oidc
from server.settings import settings
from tests.unit.oauth_store_fake import MemoryOAuthStore

_ISS = "https://idp.example.com"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", _ISS)
    monkeypatch.setattr(settings, "oauth_issuer", "https://loom.example")
    oidc._discovery_cache.clear()


@pytest.mark.parametrize(
    ("flag", "want"),
    [
        (True, "a@x.io"),
        ("true", "a@x.io"),
        ("TRUE", "a@x.io"),
        ("false", ""),
        (False, ""),
        (None, ""),
        (1, ""),
    ],
)
def test_email_counts_only_when_verified_is_really_true(flag: Any, want: str) -> None:
    assert oidc.verified_email({"email": " a@x.io ", "email_verified": flag}) == want


def _serve_discovery(monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any]) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=doc))
    real = httpx.AsyncClient

    monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


async def test_discovery_naming_another_issuer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_discovery(
        monkeypatch, {"issuer": "https://evil.example", "jwks_uri": "https://evil.example/k"}
    )
    with pytest.raises(ValueError, match="discovery issuer"):
        await oidc._discover()
    assert oidc._discovery_cache == {}


async def test_discovery_tolerates_a_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_discovery(monkeypatch, {"issuer": _ISS + "/", "jwks_uri": _ISS + "/k"})
    assert (await oidc._discover())["jwks_uri"] == _ISS + "/k"


def test_one_jwk_client_per_jwks_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str] = []

    class _Client:
        def __init__(self, uri: str, timeout: int) -> None:
            built.append(uri)

        def get_signing_key_from_jwt(self, token: str) -> Any:
            return type("K", (), {"key": "k"})()

    monkeypatch.setattr(oidc, "PyJWKClient", _Client)
    oidc._jwk_clients.clear()
    for _ in range(3):
        assert oidc._signing_key("https://idp/jwks", "t") == "k"
    assert built == ["https://idp/jwks"]


class _Req:
    """A callback from the browser that started the sign-in (its state cookie set)."""

    def __init__(self) -> None:
        self.query_params = {"code": "c", "state": "s"}
        self.cookies = dict([oauth._state_cookie("s")])


@pytest.mark.parametrize(
    ("exc", "status", "error"),
    [
        (httpx.ConnectError("idp down"), 502, "temporarily_unavailable"),
        (ValueError("secret-bearing IdP body"), 400, "oidc_error"),
    ],
)
async def test_a_failed_callback_does_not_echo_the_idp(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, status: int, error: str
) -> None:
    store = MemoryOAuthStore()
    await store.save_pending(
        "s",
        {
            "kind": "mcp",
            "nonce": "n",
            "code_verifier": "v",
            "client_id": "c",
            "redirect_uri": "r",
            "code_challenge": "x",
        },
    )
    monkeypatch.setattr(oauth, "store", store)

    async def boom(**kw: Any) -> Any:
        raise exc

    monkeypatch.setattr(oidc, "exchange_code", boom)
    with pytest.raises(oauth.OAuthError) as ei:
        await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert ei.value.status_code == status
    assert (ei.value.error, ei.value.description) == (error, "")


async def test_an_unverified_email_is_refused_at_the_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryOAuthStore()
    await store.save_pending(
        "s",
        {
            "kind": "mcp",
            "nonce": "n",
            "code_verifier": "v",
            "client_id": "c",
            "redirect_uri": "r",
            "code_challenge": "x",
        },
    )
    monkeypatch.setattr(oauth, "store", store)

    async def exchange(**kw: Any) -> dict[str, str]:
        return {"id_token": "t"}

    async def verify(token: str, *, nonce: str) -> dict[str, Any]:
        return {"email": "a@x.io", "email_verified": "false"}

    denied: list[str] = []

    async def emit(event_type: str, **kw: Any) -> str:
        denied.append(event_type)
        return ""

    monkeypatch.setattr(oidc, "exchange_code", exchange)
    monkeypatch.setattr(oidc, "verify_id_token", verify)
    monkeypatch.setattr(oauth.audit, "emit", emit)
    page = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert page.status_code == 403
    assert b"verified email required" in page.body
    assert denied == ["auth.signin_denied"]


@pytest.mark.parametrize(
    ("status", "body", "refused"),
    [
        (400, {"error": "invalid_grant"}, True),  # the user or token is gone at the IdP
        (401, {"error": "invalid_client"}, False),  # Loom's secret is wrong: not the user's fault
        (400, {"error": "invalid_request"}, False),
        (503, {}, False),
    ],
)
async def test_only_invalid_grant_ends_a_session_at_refresh(
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any], refused: bool
) -> None:
    async def token_request(form: dict[str, str]) -> dict[str, Any]:
        response = httpx.Response(status, json=body, request=httpx.Request("POST", "https://idp"))
        raise httpx.HTTPStatusError("refused", request=response.request, response=response)

    monkeypatch.setattr(oidc, "_token_request", token_request)
    expected = oidc.UpstreamRefused if refused else oidc.UpstreamUnavailable
    with pytest.raises(expected):
        await oidc.refresh("idp-refresh")


@pytest.mark.parametrize("body", [{}, [], {"access_token": "a"}, {"token_type": "Bearer"}])
async def test_a_malformed_refresh_answer_is_the_idps_fault_not_a_success(
    monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    async def discover() -> dict[str, Any]:
        return {"token_endpoint": "https://idp.example/token"}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(oidc, "_discover", discover)
    monkeypatch.setattr(
        oidc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(respond), **kw)
    )
    with pytest.raises(oidc.UpstreamUnavailable):
        await oidc.refresh("idp-refresh")
