"""RS256 asymmetric JWT support + JWKS endpoint.

Generates an RSA keypair in-test, writes it to loom.signing_keys sealed with
a private server's own root secret (as `loom signing-key rotate --algorithm
RS256` would), verifies a token signed with it, and confirms
/v1/.well-known/jwks.json returns the public key in JWK form.
"""

from __future__ import annotations

import secrets
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest

from tests.integration._resources import ch_client_from_env
from tests.integration._spawn import loom_server

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """Generate an RSA-2048 keypair as (private_pem, public_pem)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return priv, pub


_KID = f"rs-it-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def rs256_server(keypair):
    """Spawn a fresh server process whose keyring signs with the RS256 key.
    Its root secret is its own, so no other server of the suite can open the
    key row, which is deleted on teardown."""
    from server.core.secrets_at_rest import seal
    from server.settings import settings

    priv, pub = keypair
    root = secrets.token_urlsafe(48)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "jwt_secret", root)
        sealed = seal("signing_key", priv)
    now = datetime.now(UTC)  # aware: the driver reads naive as local
    ch = ch_client_from_env(settings={"async_insert": 0})
    try:
        ch.insert(
            "loom.signing_keys",
            [[_KID, "RS256", sealed, pub, now, now - timedelta(minutes=1)]],
            column_names=[
                "kid",
                "algorithm",
                "sealed_key",
                "public_pem",
                "created_at",
                "activate_at",
            ],
        )
        with loom_server("rs256", {"LOOM_JWT_SECRET": root}) as base_url:
            yield base_url, priv, pub
    finally:
        ch.command(
            "DELETE FROM loom.signing_keys WHERE kid = {kid:String}", parameters={"kid": _KID}
        )
        ch.close()


@pytest.mark.asyncio
async def test_rs256_jwks_returns_public_key(rs256_server) -> None:
    base_url, _, _ = rs256_server
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as c:
        r = await c.get("/v1/.well-known/jwks.json")
    assert r.status_code == 200
    body = r.json()
    assert len(body["keys"]) == 1
    jwk = body["keys"][0]
    assert jwk["kid"] == _KID
    assert jwk["alg"] == "RS256"
    assert jwk["kty"] == "RSA"
    assert jwk["use"] == "sig"
    assert jwk["n"]  # base64url(modulus)
    assert jwk["e"]  # base64url(exponent)


@pytest.mark.asyncio
async def test_rs256_token_verifies_via_whoami(rs256_server) -> None:
    """Mint an RS256 token in-test (using the private key), present it to
    /v1/whoami, confirm the server verifies via the public key."""
    base_url, priv, _ = rs256_server
    payload = {
        "sub": "sa-rs",
        "org": "test-org",
        "namespaces": ["t"],
        "roles": ["VIEWER"],
        "iss": "loom",
        "aud": "loom-platform",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "sa": True,
    }
    token = pyjwt.encode(payload, priv, algorithm="RS256", headers={"kid": _KID})
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=5.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r = await c.get("/v1/whoami")
    body = r.json()
    assert body["authenticated"] is True
    assert body["subject"] == "sa-rs"
    assert body["kid"] == _KID


@pytest.mark.asyncio
async def test_jwks_endpoint_serves_active_keys(control_plane_url: str) -> None:
    """`/v1/.well-known/jwks.json` returns the keyring's RS256 public keys;
    it is empty while every key is HS256."""
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/.well-known/jwks.json")
    body = r.json()
    keys = body["keys"]
    # Either populated (RS256) or empty (HS256) — both are valid; the
    # endpoint's contract is "current asymmetric public keys, if any".
    if keys:
        assert all(k["kty"] == "RSA" and k["alg"] == "RS256" for k in keys)
        assert all("kid" in k and "n" in k and "e" in k for k in keys)


@pytest.mark.asyncio
async def test_rs256_rejects_tampered_token(rs256_server) -> None:
    base_url, _, _ = rs256_server
    # A token signed with a completely different key
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rogue_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rogue_priv = rogue_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    rogue_token = pyjwt.encode(
        {
            "sub": "rogue",
            "org": "x",
            "namespaces": [],
            "roles": [],
            "iss": "loom",
            "aud": "loom-platform",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        rogue_priv,
        algorithm="RS256",
        headers={"kid": _KID},  # claim the right kid; signature won't verify
    )
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=5.0,
        headers={"Authorization": f"Bearer {rogue_token}"},
    ) as c:
        r = await c.get("/v1/whoami")
    # whoami returns 200 + authenticated=false on signature mismatch
    # (SPA-friendly UX). The verifier still rejected the token.
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body.get("reason")
