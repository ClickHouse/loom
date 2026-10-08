"""Unit coverage for OIDC-brokered login (LOOM_OAUTH_LOGIN_PROVIDER=oidc).

CH-free + IdP-free: the id_token verification runs against a locally generated
RSA keypair (the IdP signing key is injected), the email→identity mapping runs
against a fake CH, and the authorize redirect runs against the in-memory store.
The full browser round-trip is covered by tests/integration/test_oidc_flow.py
against an in-process fake IdP.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.oauth_store_fake import MemoryOAuthStore

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.auth import login, oauth, oidc, self_signup  # noqa: E402
from server.settings import settings  # noqa: E402

_ISS = "https://idp.example.com"
_CLIENT_ID = "loom-oidc-client"
# verify_id_token raises ValueError (nonce) or a PyJWT error (sig/aud/exp).
_VERIFY_ERRORS = (pyjwt.PyJWTError, ValueError)


@pytest.fixture
def _signing_key():
    """A throwaway RSA keypair; inject the public key as the IdP signing key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture(autouse=True)
def _oidc_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    monkeypatch.setattr(settings, "oauth_issuer", "https://loom.example")
    monkeypatch.setattr(settings, "oidc_issuer", _ISS)
    monkeypatch.setattr(settings, "oidc_client_id", _CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_client_secret", "shh")


def _id_token(priv: Any, **overrides: Any) -> str:
    claims = {
        "iss": _ISS,
        "aud": _CLIENT_ID,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "email": "user@example.com",
        "email_verified": True,
        "nonce": "the-nonce",
    }
    claims.update(overrides)
    return pyjwt.encode(claims, priv, algorithm="RS256")


def _inject(monkeypatch: pytest.MonkeyPatch, pub: Any) -> None:
    async def fake_discover() -> dict[str, Any]:
        return {
            "issuer": _ISS,
            "jwks_uri": f"{_ISS}/jwks",
            "authorization_endpoint": f"{_ISS}/authorize",
            "token_endpoint": f"{_ISS}/token",
        }

    monkeypatch.setattr(oidc, "_discover", fake_discover)
    monkeypatch.setattr(oidc, "_signing_key", lambda jwks_uri, token: pub)


# ── id_token verification (the security-critical bit) ──────────────────────
async def test_verify_id_token_happy_path(_signing_key, monkeypatch) -> None:
    priv, pub = _signing_key
    _inject(monkeypatch, pub)
    claims = await oidc.verify_id_token(_id_token(priv), nonce="the-nonce")
    assert claims["email"] == "user@example.com" and claims["email_verified"] is True


async def test_verify_id_token_rejects_nonce_mismatch(_signing_key, monkeypatch) -> None:
    priv, pub = _signing_key
    _inject(monkeypatch, pub)
    with pytest.raises(_VERIFY_ERRORS):
        await oidc.verify_id_token(_id_token(priv, nonce="attacker"), nonce="the-nonce")


async def test_verify_id_token_rejects_wrong_audience(_signing_key, monkeypatch) -> None:
    priv, pub = _signing_key
    _inject(monkeypatch, pub)
    with pytest.raises(_VERIFY_ERRORS):
        await oidc.verify_id_token(_id_token(priv, aud="someone-else"), nonce="the-nonce")


async def test_verify_id_token_rejects_expired(_signing_key, monkeypatch) -> None:
    priv, pub = _signing_key
    _inject(monkeypatch, pub)
    stale = _id_token(priv, exp=int(time.time()) - 10)
    with pytest.raises(_VERIFY_ERRORS):
        await oidc.verify_id_token(stale, nonce="the-nonce")


async def test_verify_id_token_rejects_wrong_signer(_signing_key, monkeypatch) -> None:
    """A token signed by a different key fails signature verification."""
    priv, pub = _signing_key
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _inject(monkeypatch, pub)  # IdP advertises `pub`...
    with pytest.raises(_VERIFY_ERRORS):
        await oidc.verify_id_token(
            _id_token(other), nonce="the-nonce"
        )  # ...token signed by `other`


# ── email → loom identity mapping ──────────────────────────────────────────
async def test_resolve_identity_for_email_maps_rows(monkeypatch) -> None:
    rows = [
        {
            "namespace": "alice",
            "owner_email": "alice",
            "org": "dev",
            "password_hash": "",
            "contact_email": "alice@example.com",
        },
        {
            "namespace": "team",
            "owner_email": "alice",
            "org": "dev",
            "password_hash": "",
            "contact_email": "alice@example.com",
        },
    ]

    class FakeCH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return rows

    monkeypatch.setattr(login, "get_client", lambda: FakeCH())
    out = await login.resolve_identity_for_email("Alice@Example.com")
    assert out is not None
    subject, org, namespaces, roles = out
    assert subject == "alice" and org == "dev"
    assert namespaces == ["alice", "team"]  # SSO-only (no password_hash) still resolves
    assert roles == ["writer"]


async def test_resolve_identity_for_email_no_account(monkeypatch) -> None:
    class FakeCH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(login, "get_client", lambda: FakeCH())
    assert await login.resolve_identity_for_email("nobody@example.com") is None


async def test_resolve_identity_rejects_cross_org_ambiguity(monkeypatch) -> None:
    """Same email in two orgs → fail closed (can't safely pick the tenant)."""
    rows = [
        {
            "namespace": "a",
            "owner_email": "x",
            "org": "org-a",
            "password_hash": "",
            "contact_email": "x@example.com",
        },
        {
            "namespace": "b",
            "owner_email": "x",
            "org": "org-b",
            "password_hash": "",
            "contact_email": "x@example.com",
        },
    ]

    class FakeCH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return rows

    monkeypatch.setattr(login, "get_client", lambda: FakeCH())
    assert await login.resolve_identity_for_email("x@example.com") is None


# ── JIT provisioning ───────────────────────────────────────────────────────
async def test_jit_off_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_jit_provision", False)
    assert await login.jit_provision_for_email("x@acme.com") is None


async def test_jit_provisions_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(settings, "oidc_jit_org", "acme")
    monkeypatch.setattr(settings, "oidc_jit_email_domains", "")  # any domain
    calls: dict[str, Any] = {}

    async def fake_provision(**kw: Any) -> None:
        calls.update(kw)

    async def fake_resolve(email: str) -> tuple[str, str, list[str], list[str]]:
        return ("u-bob-xxxx", "acme", ["u-bob-xxxx"], ["writer"])

    monkeypatch.setattr(self_signup, "provision_account", fake_provision)
    monkeypatch.setattr(login, "resolve_identity_for_email", fake_resolve)

    out = await login.jit_provision_for_email("Bob@Acme.com")
    assert out == ("u-bob-xxxx", "acme", ["u-bob-xxxx"], ["writer"])
    assert calls["org"] == "acme"
    assert calls["contact_email"] == "bob@acme.com"  # normalized
    assert calls["namespace"].startswith("u-bob-") and calls["password_hash"] == ""


async def test_jit_rejects_disallowed_domain(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(settings, "oidc_jit_org", "acme")
    monkeypatch.setattr(settings, "oidc_jit_email_domains", "acme.com")
    called = {"n": 0}

    async def fake_provision(**kw: Any) -> None:
        called["n"] += 1

    monkeypatch.setattr(self_signup, "provision_account", fake_provision)
    assert await login.jit_provision_for_email("eve@evil.com") is None
    assert called["n"] == 0  # never provisioned


def test_validate_config_jit_requires_org(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(settings, "oidc_jit_org", "")
    with pytest.raises(RuntimeError):
        oidc.validate_config()


def test_self_signup_disabled_under_oidc(monkeypatch) -> None:
    """One way in: provider=oidc closes password self-signup even if
    LOOM_SELF_SIGNUP=open — no SSO bypass."""
    monkeypatch.setattr(settings, "self_signup", "open")  # would be open...
    app = FastAPI()  # ...but the autouse fixture sets provider=oidc, which wins.
    app.include_router(self_signup.router)
    r = TestClient(app).post(
        "/v1/auth/self-signup",
        json={
            "namespace": "x",
            "email": "x@y.com",
            "password": "hunter2-pw",
        },
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "SELF_SIGNUP_SSO"


# ── boot validation ────────────────────────────────────────────────────────
def test_validate_config_requires_idp_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_client_id", "")
    with pytest.raises(RuntimeError):
        oidc.validate_config()


def test_validate_config_requires_oauth_issuer(monkeypatch) -> None:
    """OIDC callback URL must come from config, so oauth_issuer is required."""
    monkeypatch.setattr(settings, "oauth_issuer", "")
    with pytest.raises(RuntimeError):
        oidc.validate_config()


def test_validate_config_ok_when_complete() -> None:
    oidc.validate_config()  # autouse fixture sets a complete oidc config


def test_validate_config_password_provider_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "oidc_issuer", "")
    oidc.validate_config()  # no raise


# ── authorize redirects to the IdP under provider=oidc ─────────────────────
async def test_authorize_redirects_to_idp(monkeypatch) -> None:
    monkeypatch.setattr(oauth, "_store", MemoryOAuthStore())

    captured: dict[str, Any] = {}

    async def fake_build(*, redirect_uri: str, state: str, nonce: str) -> str:
        captured.update(redirect_uri=redirect_uri, state=state, nonce=nonce)
        return f"{_ISS}/authorize?state={state}&nonce={nonce}"

    monkeypatch.setattr(oidc, "build_authorize_url", fake_build)

    app = FastAPI()
    app.include_router(oauth.router)
    c = TestClient(app)
    reg = c.post(
        "/oauth/register",
        json={
            "redirect_uris": ["http://127.0.0.1:9999/cb"],
            "client_name": "T",
        },
    )
    cid = reg.json()["client_id"]

    r = c.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "code_challenge": "abc",
            "code_challenge_method": "S256",
            "state": "mcp-state",
        },
        follow_redirects=False,
    )
    assert r.status_code == 307 or r.status_code == 302, r.text
    assert r.headers["location"].startswith(f"{_ISS}/authorize")
    # The pending record carries the MCP client's params + nonce, keyed by state.
    pend = oauth._store.pending[captured["state"]]
    assert pend["client_id"] == cid and pend["mcp_state"] == "mcp-state"
    assert pend["nonce"] == captured["nonce"]
