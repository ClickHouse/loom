"""verify_token refuses every token its configured keys did not sign.

Algorithm confusion, a wrong or odd `kid`, tampering, the registered claims
and malformed Loom claims all end in TokenError — never an uncaught PyJWT
error (a 500) and never an AuthInfo built from a claim of the wrong shape.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from server.auth import revocation
from server.auth.jwt import TokenError, mint_token, verify_token
from server.settings import settings

_SECRET = "s" * 40


def _pem_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


_PRIVATE, _PUBLIC = _pem_pair()


@pytest.fixture(autouse=True)
def _hs256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(revocation, "_loaded", True)
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(settings, "jwt_secrets_json", json.dumps({"k1": _SECRET}))
    monkeypatch.setattr(settings, "jwt_active_kid", "k1")
    monkeypatch.setattr(settings, "jwt_public_keys_json", "")
    monkeypatch.setattr(settings, "jwt_trusted_issuers", "")


def _rs256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_algorithm", "RS256")
    monkeypatch.setattr(settings, "jwt_secrets_json", json.dumps({"k1": _PRIVATE}))
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"k1": _PUBLIC}))


def _claims(**over: Any) -> dict[str, Any]:
    now = int(time.time())
    base = {
        "sub": "alice",
        "org": "acme",
        "namespaces": ["shop"],
        "roles": ["writer"],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
    }
    return {k: v for k, v in (base | over).items() if v is not None}


def _hs(claims: dict[str, Any], *, key: str = _SECRET, kid: Any = "k1") -> str:
    """HS256 by hand: PyJWT itself refuses a PEM key or a non-string kid, and
    an attacker's encoder does not."""

    def seg(obj: Any) -> str:
        return jwt.utils.base64url_encode(json.dumps(obj).encode()).decode()

    signing_input = f"{seg({'alg': 'HS256', 'typ': 'JWT', 'kid': kid})}.{seg(claims)}"
    sig = hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{jwt.utils.base64url_encode(sig).decode()}"


def _refused(token: str) -> str:
    with pytest.raises(TokenError) as ei:
        verify_token(token)
    return str(ei.value)


def test_a_well_formed_token_verifies() -> None:
    assert verify_token(_hs(_claims())).namespaces == ("shop",)


def test_alg_none_is_refused() -> None:
    unsigned = jwt.encode(_claims(), None, algorithm="none")
    assert "not in accepted set" in _refused(unsigned)


def test_an_hs256_token_under_rs256_is_refused_not_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _rs256(monkeypatch)
    # Signed with the only public material an attacker has: the public key.
    assert "not in accepted set" in _refused(_hs(_claims(), key=_PUBLIC))


def test_rs256_is_refused_where_no_public_key_is_configured() -> None:
    token = jwt.encode(_claims(), _PRIVATE, algorithm="RS256", headers={"kid": "k1"})
    assert "not in accepted set" in _refused(token)


def test_a_gateway_rs256_token_verifies_beside_hs256(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"gw": _PUBLIC}))
    monkeypatch.setattr(settings, "jwt_trusted_issuers", "gateway")
    token = jwt.encode(_claims(iss="gateway"), _PRIVATE, algorithm="RS256", headers={"kid": "gw"})
    assert verify_token(token).subject == "alice"
    assert verify_token(_hs(_claims())).subject == "alice"


def test_rs256_mint_and_verify_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    _rs256(monkeypatch)
    assert verify_token(mint_token("alice", "acme", ["shop"], ["viewer"])).org == "acme"


@pytest.mark.parametrize("kid", ["nope", 7, ["k1"]])
def test_an_unknown_or_odd_kid_is_refused(kid: Any) -> None:
    _refused(_hs(_claims(), kid=kid))


def test_a_wrong_key_or_a_tampered_payload_is_refused() -> None:
    _refused(_hs(_claims(), key="t" * 40))
    head, _body, sig = _hs(_claims()).split(".")
    forged_body = jwt.utils.base64url_encode(json.dumps(_claims(org="*")).encode()).decode()
    _refused(f"{head}.{forged_body}.{sig}")


@pytest.mark.parametrize(
    "claims",
    [
        _claims(iss="elsewhere"),
        _claims(aud="another-service"),
        _claims(exp=int(time.time()) - 3600),
        _claims(iat=int(time.time()) + 3600),
        _claims(nbf=int(time.time()) + 3600),
        _claims(iat=None),
        _claims(sub=None),
    ],
    ids=["iss", "aud", "expired", "future-iat", "nbf", "no-iat", "no-sub"],
)
def test_the_registered_claims_are_enforced(claims: dict[str, Any]) -> None:
    _refused(_hs(claims))


@pytest.mark.parametrize(
    "claims",
    [
        _claims(namespaces="shop"),
        _claims(namespaces=["shop", 3]),
        _claims(ns_write="shop"),
        _claims(roles="writer"),
        _claims(roles=["superuser"]),
        _claims(ns_roles=[["shop", "admin"]]),
        _claims(org=["acme"]),
    ],
    ids=[
        "ns-str",
        "ns-int",
        "ns_write-str",
        "roles-str",
        "roles-unknown",
        "ns_roles-list",
        "org-list",
    ],
)
def test_a_malformed_loom_claim_is_refused(claims: dict[str, Any]) -> None:
    _refused(_hs(claims))


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "e30.e30."])
def test_garbage_is_a_token_error(garbage: str) -> None:
    _refused(garbage)
