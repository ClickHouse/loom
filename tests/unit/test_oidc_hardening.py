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
    def __init__(self) -> None:
        self.query_params = {"code": "c", "state": "s"}


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
        "s", {"nonce": "n", "client_id": "c", "redirect_uri": "r", "code_challenge": "x"}
    )
    monkeypatch.setattr(oauth, "_store", store)

    async def boom(**kw: Any) -> Any:
        raise exc

    monkeypatch.setattr(oidc, "exchange_code", boom)
    with pytest.raises(oauth.HTTPException) as ei:
        await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert ei.value.status_code == status
    assert ei.value.detail == {"error": error}


async def test_an_unverified_email_is_refused_at_the_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryOAuthStore()
    await store.save_pending(
        "s", {"nonce": "n", "client_id": "c", "redirect_uri": "r", "code_challenge": "x"}
    )
    monkeypatch.setattr(oauth, "_store", store)

    async def exchange(**kw: Any) -> dict[str, str]:
        return {"id_token": "t"}

    async def verify(token: str, *, nonce: str) -> dict[str, Any]:
        return {"email": "a@x.io", "email_verified": "false"}

    monkeypatch.setattr(oidc, "exchange_code", exchange)
    monkeypatch.setattr(oidc, "verify_id_token", verify)
    with pytest.raises(oauth.HTTPException) as ei:
        await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert ei.value.status_code == 403
