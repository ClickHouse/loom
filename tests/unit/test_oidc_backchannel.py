"""`LOOM_OIDC_BACKCHANNEL_BASE_URL`: Loom's own calls to the identity provider's
host go to another base URL, the issuer's host kept in `Host`.

The browser still goes to the issuer (the authorization endpoint is never
rewritten), the discovery document must still name the configured issuer, a
URL on another host goes as it is, and no call follows a redirect.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from server.auth import oidc
from server.settings import settings
from server.settings.catalog import AuthSettings

_ISS = "https://idp.example/oauth"
_BASE = "http://idp.internal:8080"
_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", _ISS)
    monkeypatch.setattr(settings, "oidc_client_id", "loom")
    monkeypatch.setattr(settings, "oidc_client_secret", "shh")
    monkeypatch.setattr(settings, "oidc_backchannel_base_url", _BASE)
    monkeypatch.setattr(oidc, "_discovery_cache", {})
    monkeypatch.setattr(oidc, "_jwk_clients", {})


def _doc(issuer: str = _ISS, **endpoints: str) -> dict[str, Any]:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{_ISS}/authorize",
        "token_endpoint": f"{_ISS}/token",
        "jwks_uri": f"{_ISS}/jwks.json",
        "revocation_endpoint": f"{_ISS}/revoke",
        **endpoints,
    }


def _serve(monkeypatch: pytest.MonkeyPatch, respond: Any) -> list[httpx.Request]:
    """Every httpx call oidc makes answered by `respond`; the requests, as sent."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)  # type: ignore[no-any-return]

    def client(**kw: Any) -> httpx.AsyncClient:
        assert kw.get("follow_redirects") is False
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(oidc.httpx, "AsyncClient", client)
    return seen


@pytest.mark.parametrize(
    "value",
    [
        "http://idp.internal:8080",
        "https://idp.internal",
        "http://10.0.0.7:80/",
        "http://[::1]:8080",
    ],
)
def test_a_base_of_a_host_and_port_is_taken(value: str) -> None:
    assert AuthSettings(oidc_backchannel_base_url=value).oidc_backchannel_base_url == value


@pytest.mark.parametrize(
    "value",
    [
        "idp.internal:8080",  # no scheme
        "ftp://idp.internal",
        "http://",
        "http://admin:hunter2@idp.internal",  # a credential: refused, never echoed
        "http://idp.internal/oauth",  # a path: the issuer's path is kept, never prefixed
        "http://idp.internal/?x=1",
        "http://idp.internal/#top",
        "http://idp.internal:port",
    ],
)
def test_a_base_that_is_not_one_is_refused_at_load(value: str) -> None:
    with pytest.raises(ValidationError, match="LOOM_OIDC_BACKCHANNEL_BASE_URL") as refused:
        AuthSettings(oidc_backchannel_base_url=value)
    # The rule, never the value (the loaded settings hide their inputs as well).
    assert "hunter2" not in refused.value.errors()[0]["msg"]


async def test_discovery_goes_to_the_base_and_names_the_issuers_host(monkeypatch) -> None:
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, json=_doc()))
    doc = await oidc._discover()
    [request] = seen
    assert str(request.url) == f"{_BASE}/oauth/.well-known/openid-configuration"
    assert request.headers["host"] == "idp.example"
    # The browser still goes to the issuer.
    assert doc["authorization_endpoint"] == f"{_ISS}/authorize"
    url = await oidc.build_authorize_url(
        redirect_uri="https://loom.example/oauth/callback",
        state="s",
        nonce="n",
        code_challenge="c",
        offline=False,
    )
    assert url.startswith(f"{_ISS}/authorize?")


async def test_the_issuer_is_still_the_configured_one(monkeypatch) -> None:
    """A document naming the base's own address as its issuer is refused: id
    tokens are checked against the issuer the browser signs in at."""
    _serve(monkeypatch, lambda request: httpx.Response(200, json=_doc(issuer=f"{_BASE}/oauth")))
    with pytest.raises(ValueError, match="discovery issuer"):
        await oidc._discover()
    assert oidc._discovery_cache == {}


async def test_the_token_and_revocation_calls_go_to_the_base(monkeypatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_doc())
        return httpx.Response(200, json={"access_token": "a", "token_type": "Bearer"})

    seen = _serve(monkeypatch, respond)
    assert await oidc.refresh("idp-refresh") == "idp-refresh"
    await oidc.revoke("idp-refresh")
    calls = [(str(r.url), r.headers["host"]) for r in seen[1:]]
    assert calls == [
        (f"{_BASE}/oauth/token", "idp.example"),
        (f"{_BASE}/oauth/revoke", "idp.example"),
    ]
    assert b"client_secret=shh" in seen[1].content  # the secret reaches the base alone


async def test_an_endpoint_on_another_host_goes_as_it_is(monkeypatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(200, json=_doc(token_endpoint="https://tokens.example/t"))
        return httpx.Response(200, json={"access_token": "a", "token_type": "Bearer"})

    seen = _serve(monkeypatch, respond)
    await oidc.refresh("r")
    assert str(seen[1].url) == "https://tokens.example/t"
    assert seen[1].headers["host"] == "tokens.example"


async def test_no_call_follows_a_redirect(monkeypatch) -> None:
    moved = httpx.Response(302, headers={"location": "https://idp.example/oauth/elsewhere"})
    seen = _serve(monkeypatch, lambda request: moved)
    with pytest.raises(httpx.HTTPStatusError):
        await oidc._discover()
    assert len(seen) == 1 and oidc._discovery_cache == {}

    def token_moved(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_doc()) if "openid" in request.url.path else moved

    seen = _serve(monkeypatch, token_moved)
    with pytest.raises(oidc.UpstreamUnavailable):
        await oidc.refresh("r")
    assert len(seen) == 2


async def test_without_a_base_the_calls_go_to_the_issuer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_backchannel_base_url", "")
    seen = _serve(monkeypatch, lambda request: httpx.Response(200, json=_doc()))
    await oidc._discover()
    assert str(seen[0].url) == f"{_ISS}/.well-known/openid-configuration"
    assert seen[0].headers["host"] == "idp.example"


# ── the signing keys, fetched by PyJWT over a real socket ──────────────────


class _Keys(BaseHTTPRequestHandler):
    jwks: dict[str, Any] = {}
    seen: list[tuple[str, str]] = []

    def do_GET(self) -> None:
        self.seen.append((self.path, self.headers.get("Host", "")))
        if self.path == "/oauth/jwks.json":
            body = json.dumps(self.jwks).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:  # anything else moves away
            self.send_response(302)
            self.send_header("Location", "http://elsewhere.example/oauth/jwks.json")
            self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def key_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Keys)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(settings, "oidc_backchannel_base_url", base)
    _Keys.seen = []
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def _es256() -> tuple[Any, dict[str, Any]]:
    key = ec.generate_private_key(ec.SECP256R1())
    public = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return key, {**public, "kid": "k1", "use": "sig", "alg": "ES256"}


async def test_the_signing_keys_come_through_the_base(key_server: str, monkeypatch) -> None:
    key, public = _es256()
    _Keys.jwks = {"keys": [public]}

    async def discover() -> dict[str, Any]:
        return {**_doc(), "id_token_signing_alg_values_supported": ["ES256"]}

    monkeypatch.setattr(oidc, "_discover", discover)
    now = int(time.time())
    claims = {"iss": _ISS, "aud": "loom", "iat": now, "exp": now + 300, "nonce": "n"}
    token = jwt.encode(claims, key, algorithm="ES256", headers={"kid": "k1"})
    assert (await oidc.verify_id_token(token, nonce="n"))["iss"] == _ISS
    assert _Keys.seen == [("/oauth/jwks.json", "idp.example")]


def test_a_signing_key_fetch_follows_no_redirect(key_server: str) -> None:
    key, _ = _es256()
    token = jwt.encode({"sub": "x"}, key, algorithm="ES256", headers={"kid": "k1"})
    with pytest.raises(jwt.PyJWKClientConnectionError):
        oidc.signing_key(f"{_ISS}/moved", token)
    assert _Keys.seen == [("/oauth/moved", "idp.example")]  # asked once, never followed
