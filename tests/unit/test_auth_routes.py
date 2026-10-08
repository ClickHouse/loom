"""The token-introspection routes: the JWKS an RS256 verifier reads and
`/v1/whoami`. Characterized on a bare app with only these routes attached."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import server.auth.revocation as rev
from server.auth import routes
from server.auth.jwt import RevocationUnavailable, mint_token
from server.settings import settings


def _client() -> TestClient:
    app = FastAPI()
    routes.attach(app)
    return TestClient(app)


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _ec_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def test_jwks_is_empty_under_hs256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    assert _client().get("/v1/.well-known/jwks.json").json() == {"keys": []}


def test_jwks_lists_one_rsa_key_per_kid_and_skips_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = {"k1": _rsa_pem(), "k2": _rsa_pem(), "ec": _ec_pem(), "bad": "not a pem"}
    monkeypatch.setattr(settings, "jwt_algorithm", "RS256")
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps(keys))
    body = _client().get("/v1/.well-known/jwks.json").json()
    assert [k["kid"] for k in body["keys"]] == ["k1", "k2"]
    for k in body["keys"]:
        assert {k["kty"], k["use"], k["alg"]} == {"RSA", "sig", "RS256"}
        assert k["e"] == "AQAB" and "=" not in k["n"]


def test_whoami_without_a_bearer_is_unauthenticated() -> None:
    assert _client().get("/v1/whoami").json() == {"authenticated": False}
    # A credential Loom cannot read is answered like a bad token: with the reason.
    r = _client().get("/v1/whoami", headers={"Authorization": "Basic x"})
    assert r.status_code == 200 and r.json()["authenticated"] is False and r.json()["reason"]


def test_whoami_with_a_bad_token_is_a_200_with_the_reason() -> None:
    r = _client().get("/v1/whoami", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False and body["reason"]


def test_whoami_while_revocation_is_unloaded_is_a_retryable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unavailable(_token: str) -> None:
        raise RevocationUnavailable("revocation list not loaded")

    monkeypatch.setattr(routes, "verify_token", _unavailable)
    r = _client().get("/v1/whoami", headers={"Authorization": "Bearer t"})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "5"
    assert r.json()["detail"]["code"] == "AUTH_UNAVAILABLE"


def test_whoami_with_a_valid_token_returns_its_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rev, "_loaded", True)
    token = mint_token(subject="alice", org="o", namespaces=["n"], roles=["writer"])
    body = _client().get("/v1/whoami", headers={"Authorization": f"Bearer {token}"}).json()
    assert body["authenticated"] is True
    assert (body["subject"], body["org"], body["namespaces"]) == ("alice", "o", ["n"])
    assert body["roles"] == ["WRITER"] and body["is_service_account"] is True
    assert isinstance(body["iat"], int)
    assert set(body) == {
        "authenticated",
        "subject",
        "org",
        "namespaces",
        "roles",
        "is_service_account",
        "kid",
        "exp",
        "iat",
    }


def test_the_app_serves_the_routes() -> None:
    from server.main import app

    paths = app.openapi()["paths"]
    assert set(paths["/v1/.well-known/jwks.json"]) == {"get"}
    assert set(paths["/v1/whoami"]) == {"get"}
