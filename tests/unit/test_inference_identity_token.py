"""The identity token Loom signs itself for an inference gateway's token
exchange (`LOOM_INFERENCE_TOKEN_AUDIENCE`): what it says, the key it is signed
with, the documents a gateway verifies it against, and what Loom refuses."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import keyring
from server.auth.jwt import TokenError, verify_token
from server.auth.routes import auth_router
from server.core.secrets_at_rest import seal
from server.llm import gateway
from server.settings import Settings, settings

_GW = "https://gateway.example/v1/chat/completions"
_AUDIENCE = "loom-inference"


class _Keys:
    """loom.signing_keys as `keyring.refresh` reads it."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return self.rows


def _row(kid: str, algorithm: str, *, active: bool = True) -> tuple[dict[str, Any], str]:
    secret, public = keyring._new_key(algorithm)
    at = keyring._now() + (timedelta(hours=-1) if active else timedelta(hours=1))
    row = {
        "kid": kid,
        "algorithm": algorithm,
        "sealed_key": seal("signing_key", secret),
        "public_pem": public,
        "activate_at": at,
        "retired": False,
    }
    return row, public


def _load(*rows: dict[str, Any]) -> None:
    asyncio.run(keyring.refresh(ch=_Keys(list(rows))))


class _Gateway:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def post(self, url: str, *, data: dict[str, str], **_: Any) -> httpx.Response:
        self.sent.append(data)
        body = {"access_token": f"gw-{len(self.sent)}", "expires_in": 900}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def signs(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Gateway]:
    monkeypatch.setattr(settings, "jwt_secret", "r" * 40)
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    monkeypatch.setattr(settings, "inference_token_url", "https://gateway.example/oauth/token")
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    monkeypatch.setattr(settings, "inference_workload_token_file", "")
    monkeypatch.setattr(settings, "inference_token_audience", _AUDIENCE)
    fake = _Gateway()
    monkeypatch.setattr("server.clients._http.get_model_client", lambda: fake)
    keyring._reset_for_tests()
    gateway._reset_for_tests()
    yield fake
    gateway._reset_for_tests()
    keyring._reset_for_tests()


def _jwks() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(auth_router)
    return dict(TestClient(app).get("/v1/.well-known/jwks.json").json())


def test_the_token_it_exchanges_is_signed_by_its_newest_rs256_key(signs: _Gateway) -> None:
    hs, _ = _row("h1", "HS256")
    rs, _ = _row("r1", "RS256")
    pending, _ = _row("r2", "RS256", active=False)  # verifies, does not sign yet
    _load(hs, rs, pending)
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    [sent] = signs.sent
    token = sent["subject_token"]
    assert pyjwt.get_unverified_header(token)["kid"] == "r1"
    # What a gateway trusting Loom's issuer does: the key from the published set.
    jwk = next(k for k in _jwks()["keys"] if k["kid"] == "r1")
    key = pyjwt.PyJWK.from_json(json.dumps(jwk)).key
    claims = pyjwt.decode(token, key, algorithms=["RS256"], audience=_AUDIENCE)
    assert claims["iss"] == "https://loom.example/v1" and claims["sub"] == "loom"
    assert 0 < claims["exp"] - claims["iat"] <= gateway.IDENTITY_TOKEN_LIFE_S
    assert claims["jti"]


def test_a_token_is_new_at_each_exchange(signs: _Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    rs, _ = _row("r1", "RS256")
    _load(rs)
    now = [1000.0]
    monkeypatch.setattr(gateway.time, "time", lambda: now[0])
    asyncio.run(gateway.bearer("", _GW))
    now[0] += 900  # the gateway's token ran out
    asyncio.run(gateway.bearer("", _GW))
    first, second = (s["subject_token"] for s in signs.sent)
    assert first != second


def test_with_no_rs256_key_there_is_no_token_and_the_log_says_what_to_do(
    signs: _Gateway, caplog: pytest.LogCaptureFixture
) -> None:
    hs, _ = _row("h1", "HS256")
    _load(hs)
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))
    assert signs.sent == []  # an HS256 secret never leaves this deployment
    assert "loom signing-key rotate --algorithm RS256" in caplog.text


def test_loom_itself_refuses_the_identity_token(signs: _Gateway) -> None:
    rs, _ = _row("r1", "RS256")
    _load(rs)
    with pytest.raises(TokenError):
        verify_token(gateway.signed_identity_token())


def _discovery() -> httpx.Response:
    app = FastAPI()
    app.include_router(auth_router)
    return TestClient(app).get("/v1/.well-known/openid-configuration")


def test_the_discovery_document_names_the_issuer_and_its_keys(signs: _Gateway) -> None:
    r = _discovery()
    assert r.status_code == 200
    doc = r.json()
    assert doc["issuer"] == "https://loom.example/v1"
    assert doc["jwks_uri"] == "https://loom.example/v1/.well-known/jwks.json"
    assert doc["id_token_signing_alg_values_supported"] == ["RS256"]
    assert "authorization_endpoint" not in doc  # it issues nothing to people


def test_no_discovery_document_while_loom_signs_no_identity_token(
    signs: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "inference_token_audience", "")  # the exchange stays on
    assert _discovery().status_code == 404


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "needs one of"),  # neither a workload token nor an audience
        (
            {
                "LOOM_INFERENCE_TOKEN_AUDIENCE": _AUDIENCE,
                "LOOM_INFERENCE_WORKLOAD_TOKEN_FILE": "/t",
            },
            "needs one of",
        ),
    ],
)
def test_the_exchange_takes_exactly_one_token_source(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    monkeypatch.setenv("LOOM_INFERENCE_TOKEN_URL", "https://gateway.example/oauth/token")
    monkeypatch.setenv("LOOM_INFERENCE_GATEWAY_HOSTS", "gateway.example")
    monkeypatch.setenv("LOOM_OPENAI_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LOOM_ANTHROPIC_BASE_URL", "https://gateway.example")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None)  # type: ignore[call-arg]


_SIGNING_ENV = {
    "LOOM_INFERENCE_TOKEN_URL": "https://gateway.example/oauth/token",
    "LOOM_INFERENCE_GATEWAY_HOSTS": "gateway.example",
    "LOOM_INFERENCE_TOKEN_AUDIENCE": _AUDIENCE,
    "LOOM_PUBLIC_BASE_URL": "https://loom.example",
    "LOOM_OPENAI_BASE_URL": "https://gateway.example/v1",
    "LOOM_ANTHROPIC_BASE_URL": "https://gateway.example",
}


def _load_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name, value in {**_SIGNING_ENV, **env}.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"LOOM_PUBLIC_BASE_URL": ""}, "needs LOOM_PUBLIC_BASE_URL"),
        ({"LOOM_PUBLIC_BASE_URL": "http://loom.example"}, "https"),
        # Loom's own tokens carry this audience, signed by the same RS256 keys.
        ({"LOOM_INFERENCE_TOKEN_AUDIENCE": "loom-platform"}, "LOOM_JWT_AUDIENCE"),
        ({"LOOM_JWT_TRUSTED_ISSUERS": "https://loom.example/v1"}, "own verifier trusts"),
        ({"LOOM_JWT_ISSUER": "https://loom.example/v1"}, "own verifier trusts"),
        ({"LOOM_INFERENCE_TOKEN_URL": ""}, "needs LOOM_INFERENCE_TOKEN_URL"),
    ],
)
def test_no_process_loads_signing_tokens_loom_or_a_wire_tap_could_misuse(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    # Every process loads settings, a CLI command calling models too.
    monkeypatch.setenv("LOOM_JWT_AUDIENCE", "loom-platform")
    with pytest.raises(ValueError, match=message):
        _load_settings(monkeypatch, **env)


def test_a_loopback_http_address_may_sign(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = _load_settings(monkeypatch, LOOM_PUBLIC_BASE_URL="http://127.0.0.1:7777/")
    assert loaded.inference_issuer() == "http://127.0.0.1:7777/v1"


def test_a_process_running_no_refresh_loop_reads_the_keyring_to_sign(
    signs: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CLI command calling models (`loom reembed`) never started the loop.
    rs, _ = _row("r1", "RS256")
    keyring._reset_for_tests(loaded=False)
    real = keyring.refresh
    monkeypatch.setattr(keyring, "refresh", lambda **_: real(ch=_Keys([rs])))
    assert asyncio.run(gateway.bearer("", _GW)) == "gw-1"
    assert pyjwt.get_unverified_header(signs.sent[0]["subject_token"])["kid"] == "r1"


def test_a_keyring_that_cannot_be_read_is_an_outage(
    signs: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring._reset_for_tests(loaded=False)

    async def away(**_: Any) -> bool:
        return False  # the read failed; the keyring stays unloaded

    monkeypatch.setattr(keyring, "refresh", away)
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))
    assert signs.sent == []


def test_a_stale_keyring_that_cannot_be_read_again_is_an_outage(
    signs: _Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    rs, _ = _row("r1", "RS256")
    _load(rs)
    monkeypatch.setattr(keyring, "stale", lambda: True)  # read too long ago

    async def away(**_: Any) -> bool:
        return False

    monkeypatch.setattr(keyring, "refresh", away)
    with pytest.raises(gateway.GatewayUnavailable):
        asyncio.run(gateway.bearer("", _GW))
    assert signs.sent == []  # nothing signed from a view a retire may have overtaken


def test_the_keys_stay_reachable_while_a_model_change_drains() -> None:
    from server.transport.model_maintenance import _control_request

    for path in ("/v1/.well-known/jwks.json", "/v1/.well-known/openid-configuration"):
        assert _control_request({"path": path, "method": "GET"})


def test_the_documented_self_signed_block_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    # docs/getting-started/models-and-keys.md, "Without a workload token".
    doc = (Path(__file__).parents[2] / "docs/getting-started/models-and-keys.md").read_text()
    block = doc.split("LOOM_INFERENCE_TOKEN_AUDIENCE=", 1)[1].split("```", 1)[0]
    head = doc.split("LOOM_INFERENCE_TOKEN_AUDIENCE=", 1)[0].rsplit("```", 1)[1]
    for line in (head + "LOOM_INFERENCE_TOKEN_AUDIENCE=" + block).splitlines():
        name, _, value = line.split("#", 1)[0].strip().partition("=")
        if name.startswith("LOOM_"):
            monkeypatch.setenv(name, value.strip().strip('"'))
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]
    assert loaded.inference_token_audience and not loaded.inference_workload_token_file
