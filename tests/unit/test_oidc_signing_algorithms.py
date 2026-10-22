"""An id_token is verified with the algorithms the identity provider's discovery
document lists, among asymmetric ones: an ES256 provider signs Loom in, a token
signed otherwise than it lists is refused, and `none` or an HMAC never passes."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from server.auth import oidc
from server.settings import settings

_ISS = "https://idp.example.com"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", _ISS)
    monkeypatch.setattr(settings, "oidc_client_id", "loom")
    monkeypatch.setattr(oidc, "_jwk_clients", {})


@pytest.mark.parametrize(
    ("listed", "accepted"),
    [
        (None, ["RS256"]),  # OIDC's default when the document says nothing
        ([], ["RS256"]),
        ("ES256", ["RS256"]),  # not a list: nothing is read from it
        (["ES256"], ["ES256"]),
        (["ES256", "RS256", "PS256"], ["RS256", "PS256", "ES256"]),
        (["none", "HS256", "RS256"], ["RS256"]),
    ],
)
def test_the_listed_algorithms_are_taken_among_the_asymmetric_ones(
    listed: Any, accepted: list[str]
) -> None:
    doc = {} if listed is None else {"id_token_signing_alg_values_supported": listed}
    assert oidc.id_token_algorithms(doc) == accepted


@pytest.mark.parametrize("listed", [["HS256"], ["none"], ["HS512", "none"], ["EdDSA"]])
def test_a_provider_listing_only_others_signs_no_one_in(listed: list[str]) -> None:
    with pytest.raises(ValueError, match="none of RS256"):
        oidc.id_token_algorithms({"id_token_signing_alg_values_supported": listed})


def _keys(monkeypatch: pytest.MonkeyPatch, listed: list[str] | None, *public: dict[str, Any]):
    """The provider's discovery document, listing `listed`, and its JWKS of `public`."""

    async def discover() -> dict[str, Any]:
        doc = {"issuer": _ISS, "jwks_uri": f"{_ISS}/jwks"}
        if listed is not None:
            doc["id_token_signing_alg_values_supported"] = listed
        return doc

    monkeypatch.setattr(oidc, "_discover", discover)
    monkeypatch.setattr(oidc.PyJWKClient, "fetch_data", lambda self: {"keys": list(public)})


def _token(key: Any, algorithm: str, kid: str, **claims: Any) -> str:
    now = int(time.time())
    body = {"iss": _ISS, "aud": "loom", "iat": now, "exp": now + 300, "nonce": "n", **claims}
    return jwt.encode(body, key, algorithm=algorithm, headers={"kid": kid})


def _ec() -> tuple[Any, dict[str, Any]]:
    key = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return key, {**jwk, "kid": "ec1", "use": "sig"}


def _rsa() -> tuple[Any, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return key, {**jwk, "kid": "rsa1", "use": "sig"}


async def test_an_es256_provider_signs_loom_in(monkeypatch) -> None:
    key, public = _ec()
    _keys(monkeypatch, ["ES256"], public)
    claims = await oidc.verify_id_token(_token(key, "ES256", "ec1"), nonce="n")
    assert claims["iss"] == _ISS and claims["aud"] == "loom"


async def test_a_token_signed_otherwise_than_listed_is_refused(monkeypatch) -> None:
    ec_key, ec_public = _ec()
    rsa_key, rsa_public = _rsa()
    _keys(monkeypatch, ["ES256"], ec_public, rsa_public)
    with pytest.raises(jwt.InvalidAlgorithmError):
        await oidc.verify_id_token(_token(rsa_key, "RS256", "rsa1"), nonce="n")
    # Listing nothing means RS256 alone: an ES256 token is refused then.
    _keys(monkeypatch, None, ec_public, rsa_public)
    with pytest.raises(jwt.InvalidAlgorithmError):
        await oidc.verify_id_token(_token(ec_key, "ES256", "ec1"), nonce="n")
    assert await oidc.verify_id_token(_token(rsa_key, "RS256", "rsa1"), nonce="n")


async def test_an_hmac_or_unsigned_token_never_passes(monkeypatch) -> None:
    _, public = _rsa()
    _keys(monkeypatch, ["RS256", "HS256", "none"], public)
    forged = jwt.encode(
        {"iss": _ISS, "aud": "loom", "nonce": "n"}, "a-guessed-secret", algorithm="HS256"
    )
    unsigned = jwt.encode({"iss": _ISS, "aud": "loom", "nonce": "n"}, None, algorithm="none")
    for token in (forged, unsigned):
        with pytest.raises((jwt.PyJWTError, ValueError)):
            await oidc.verify_id_token(token, nonce="n")
