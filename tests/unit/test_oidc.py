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

from tests.unit.claims_fake import memory_claims
from tests.unit.oauth_store_fake import MemoryOAuthStore

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.auth import jit, login, oauth, oidc, revocation, self_signup  # noqa: E402
from server.settings import settings  # noqa: E402

pytestmark = pytest.mark.usefixtures("subjects_not_revoked")

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
def _claims_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JIT sign-up's email claim, without a lock table in the fake."""
    memory_claims(monkeypatch)


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
    assert await jit.jit_provision_for_email("x@acme.com") is None


async def test_jit_provisions_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(settings, "oidc_jit_org", "acme")
    monkeypatch.setattr(settings, "oidc_jit_email_domains", "")  # any domain
    calls: dict[str, Any] = {}

    async def fake_provision(**kw: Any) -> None:
        calls.update(kw)

    async def fake_resolve(email: str) -> login.Identity:
        return ("u-bob-xxxx", "acme", ["u-bob-xxxx"], ["writer"])

    async def none_registered(org: str, namespace: str) -> None:
        return None

    monkeypatch.setattr(self_signup, "provision_account", fake_provision)
    monkeypatch.setattr(login, "resolve_identity_for_email", fake_resolve)
    monkeypatch.setattr(jit, "_registry_row", none_registered)

    async def no_accounts(org: str, ident: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(jit, "_accounts_naming", no_accounts)

    out = await jit.jit_provision_for_email("Bob@Acme.com")
    assert out == ("u-bob-xxxx", "acme", ["u-bob-xxxx"], ["writer"])
    assert calls["org"] == "acme"
    assert calls["contact_email"] == "bob@acme.com"  # normalized
    assert calls["namespace"].startswith("u-bob-") and calls["password_hash"] == ""


def _jit_on(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(settings, "oidc_jit_org", "acme")
    monkeypatch.setattr(settings, "oidc_jit_email_domains", "")
    provisioned: dict[str, Any] = {}

    async def fake_provision(**kw: Any) -> None:
        provisioned.update(kw)

    async def fake_resolve(email: str) -> login.Identity:
        return ("someone", "acme", ["someone"], ["admin"])

    async def no_accounts(org: str, ident: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(self_signup, "provision_account", fake_provision)
    monkeypatch.setattr(login, "resolve_identity_for_email", fake_resolve)
    monkeypatch.setattr(jit, "_accounts_naming", no_accounts)
    return provisioned


@pytest.mark.parametrize(
    "taken",
    [
        {"active": False, "contact_email": ""},  # released: claim-release clears the contact
        {"active": False, "contact_email": "bob@acme.com"},  # deleted, by its owner perhaps
        {"active": True, "contact_email": "mallory@acme.com"},  # another's
    ],
)
async def test_jit_never_rebuilds_a_taken_name_and_takes_the_next(
    monkeypatch, taken: dict[str, Any]
) -> None:
    # Rebuilding a released or deleted account would hand back its data and an
    # admin grant; one another account registered is theirs.
    provisioned = _jit_on(monkeypatch)
    rows: dict[str, dict[str, Any]] = {}

    async def registry_row(org: str, namespace: str) -> dict[str, Any] | None:
        if not rows:
            rows[namespace] = taken  # the first name tried
        return rows.get(namespace)

    monkeypatch.setattr(jit, "_registry_row", registry_row)
    assert await jit.jit_provision_for_email("bob@acme.com") is not None
    (first,) = rows
    assert provisioned["namespace"] == f"{first}-2"


async def test_jit_refuses_a_person_whose_account_was_revoked(monkeypatch) -> None:
    # Offboarding is a revoke-token of the subject: a fresh name would be a
    # new subject past it. Every inactive account naming the email counts,
    # whatever its name, in one read.
    provisioned = _jit_on(monkeypatch)
    asked: list[list[str]] = []

    async def accounts(org: str, ident: str) -> list[dict[str, Any]]:
        return [
            {"namespace": "u-bob-1234abcd-9f3e", "owner": "u-bob-1234abcd-9f3e", "active": False},
            {"namespace": "bob", "owner": "bob", "active": False},
        ]

    async def revoked(subjects: list[str], *, issued_at: int) -> bool:
        asked.append(sorted(set(subjects)))
        return "bob" in subjects

    monkeypatch.setattr(jit, "_accounts_naming", accounts)
    monkeypatch.setattr(revocation, "subjects_revoked", revoked)
    assert await jit.jit_provision_for_email("bob@acme.com") is None
    assert provisioned == {}
    assert asked == [["bob", "u-bob-1234abcd-9f3e"]]


async def test_a_deleted_account_alone_is_no_offboarding(monkeypatch) -> None:
    # Its owner may have deleted it: the person gets a fresh name.
    provisioned = _jit_on(monkeypatch)

    async def accounts(org: str, ident: str) -> list[dict[str, Any]]:
        return [{"namespace": "u-bob-1234abcd", "owner": "u-bob-1234abcd", "active": False}]

    async def not_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return False

    async def registry_row(org: str, namespace: str) -> dict[str, Any] | None:
        return (
            {"active": False, "contact_email": "bob@acme.com"}
            if namespace.count("-") == 2
            else None
        )

    monkeypatch.setattr(jit, "_accounts_naming", accounts)
    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    monkeypatch.setattr(jit, "_registry_row", registry_row)
    assert await jit.jit_provision_for_email("bob@acme.com") is not None
    assert provisioned["namespace"].endswith("-2")


async def test_jit_takes_a_random_name_once_the_predictable_ones_are_taken(
    monkeypatch,
) -> None:
    # Someone registered every name a sign-up would try: a random one cannot
    # be registered ahead of it, so the person is not locked out.
    provisioned = _jit_on(monkeypatch)
    tried: list[str] = []

    async def registry_row(org: str, namespace: str) -> dict[str, Any] | None:
        tried.append(namespace)
        if len(tried) <= jit._JIT_SUFFIX_TRIES + 2:  # the predictable ones, then one random
            return {"active": True, "contact_email": "mallory@acme.com"}
        return None

    monkeypatch.setattr(jit, "_registry_row", registry_row)
    assert await jit.jit_provision_for_email("bob@acme.com") is not None
    name = provisioned["namespace"]
    assert name == tried[-1], "the random name is checked before it is used"
    assert name.startswith(tried[0] + "-") and name not in tried[:-1]


@pytest.mark.parametrize("resolves", [True, False])
async def test_an_active_account_of_the_person_is_resolved_never_provisioned_again(
    monkeypatch, resolves: bool
) -> None:
    # Whatever its name (a random suffix, one a concurrent first sign-in just
    # wrote): a sign-in that still does not resolve (the cross-org guard)
    # fails rather than write another account on every attempt.
    provisioned = _jit_on(monkeypatch)

    async def accounts(org: str, ident: str) -> list[dict[str, Any]]:
        return [
            {
                "namespace": "u-bob-1234abcd-9f3e",
                "owner": "u-bob-1234abcd-9f3e",
                "active": True,
                "tags": [],
            }
        ]

    async def resolve(email: str) -> login.Identity | None:
        return (
            ("u-bob-1234abcd-9f3e", "acme", ["u-bob-1234abcd-9f3e"], ["admin"])
            if resolves
            else None
        )

    monkeypatch.setattr(jit, "_accounts_naming", accounts)
    monkeypatch.setattr(login, "resolve_identity_for_email", resolve)
    out = await jit.jit_provision_for_email("Bob@acme.com")
    assert (out is not None) is resolves
    assert provisioned == {}


async def test_jit_signs_in_to_its_own_account_another_process_just_wrote(monkeypatch) -> None:
    # The account read found nothing; by the walk, a first sign-in elsewhere
    # wrote the base name for this email: sign in to it, provision nothing.
    provisioned = _jit_on(monkeypatch)

    async def registry_row(org: str, namespace: str) -> dict[str, Any] | None:
        return {"active": True, "contact_email": "bob@acme.com"}

    monkeypatch.setattr(jit, "_registry_row", registry_row)
    assert await jit.jit_provision_for_email("bob@acme.com") is not None
    assert provisioned == {}


async def test_two_first_sign_ins_in_one_process_make_one_account(monkeypatch) -> None:
    import asyncio

    _jit_on(monkeypatch)
    written: dict[str, dict[str, Any]] = {}
    made: list[str] = []

    async def registry_row(org: str, namespace: str) -> dict[str, Any] | None:
        return written.get(namespace)

    async def accounts(org: str, ident: str) -> list[dict[str, Any]]:
        return [{**r, "namespace": ns, "owner": ns, "tags": []} for ns, r in written.items()]

    async def provision(**kw: Any) -> None:
        await asyncio.sleep(0.01)  # the insert takes a moment
        made.append(kw["namespace"])
        written[kw["namespace"]] = {"active": True, "contact_email": kw["contact_email"]}

    monkeypatch.setattr(jit, "_registry_row", registry_row)
    monkeypatch.setattr(jit, "_accounts_naming", accounts)
    monkeypatch.setattr(self_signup, "provision_account", provision)
    await asyncio.gather(*(jit.jit_provision_for_email("bob@acme.com") for _ in range(2)))
    assert len(made) == 1


async def test_jit_rejects_disallowed_domain(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(settings, "oidc_jit_org", "acme")
    monkeypatch.setattr(settings, "oidc_jit_email_domains", "acme.com")
    called = {"n": 0}

    async def fake_provision(**kw: Any) -> None:
        called["n"] += 1

    monkeypatch.setattr(self_signup, "provision_account", fake_provision)
    assert await jit.jit_provision_for_email("eve@evil.com") is None
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


async def test_a_jit_read_that_fails_is_a_retryable_sign_in_failure(monkeypatch) -> None:
    _jit_on(monkeypatch)

    async def down(org: str, ident: str) -> list[dict[str, Any]]:
        raise RuntimeError("replica away")

    monkeypatch.setattr(jit, "_accounts_naming", down)
    with pytest.raises(login.SignInUnavailable):
        await jit.jit_provision_for_email("bob@acme.com")


@pytest.mark.parametrize("granted_before", [False, True])
async def test_signing_in_finishes_a_jit_account_whose_owner_grant_never_landed(
    monkeypatch, granted_before: bool
) -> None:
    # Provisioning wrote the namespace row, then failed on the grant. The next
    # OIDC sign-in resolves that row as the person's account, so the callback
    # finishes it first. A grant revoked or lowered since leaves a row, and
    # is never given back.
    from tests.unit.oauth_store_fake import MemoryOAuthStore

    _jit_on(monkeypatch)
    granted: list[tuple[str, str]] = []
    store = MemoryOAuthStore()
    await store.save_pending(
        "s", {"nonce": "n", "client_id": "c", "redirect_uri": "http://r/cb", "code_challenge": "x"}
    )

    async def accounts(org: str, ident: str) -> list[dict[str, Any]]:
        return [{"namespace": "u-bob", "owner": "u-bob", "active": True, "tags": [jit.JIT_TAG]}]

    async def ever_granted(org: str, account: dict[str, Any]) -> bool:
        return granted_before

    async def grant(org: str, namespace: str, subject: str, **_k: Any) -> None:
        granted.append((namespace, subject))

    async def exchange(**_k: Any) -> dict[str, str]:
        return {"id_token": "t"}

    async def claims(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"email": "bob@acme.com", "email_verified": True}

    class _Req:
        query_params = {"code": "c", "state": "s"}

    monkeypatch.setattr(oauth, "_store", store)
    monkeypatch.setattr(oidc, "exchange_code", exchange)
    monkeypatch.setattr(oidc, "verify_id_token", claims)
    monkeypatch.setattr(oidc, "verified_email", lambda c: "bob@acme.com")
    monkeypatch.setattr(jit, "_accounts_naming", accounts)
    monkeypatch.setattr(jit, "_ever_granted", ever_granted)
    monkeypatch.setattr(self_signup, "insert_owner_grant", grant)
    assert (await oauth.oidc_callback(_Req())).status_code == 302  # type: ignore[arg-type]
    assert granted == ([] if granted_before else [("u-bob", "u-bob")])
