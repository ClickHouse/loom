"""End-to-end OIDC-brokered login (LOOM_OAUTH_LOGIN_PROVIDER=oidc).

Spawns a loom server pointed at an in-process **fake OIDC IdP** and drives the
full browser round-trip an MCP client + IdP perform:

  register → GET loom/authorize → 302 to IdP → IdP 302 to loom/callback →
  loom exchanges the IdP code, verifies the id_token, maps the verified email
  to a loom account → 302 to the MCP client with loom's code → loom/token
  (PKCE) → the issued loom token authenticates whoami, subject-aligned.

Two variants via the parametric `oidc_stack` fixture: a pre-seeded account
(the default) and JIT provisioning (no account → first login auto-creates one).
A fake IdP can't validate against a real Okta's quirks, but it exercises loom's
entire brokering path (redirect, code exchange, id_token signature/iss/aud/nonce
verification via the IdP's JWKS, email mapping, account provisioning, loom code
issuance) for real.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import secrets
import sys
import urllib.parse
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from tests.integration._fake_idp import CLIENT_ID, CLIENT_SECRET, FakeIdP  # noqa: E402
from tests.integration._ports import free_port  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402
from tests.integration._spawn import loom_server  # noqa: E402

_MCP_REDIRECT = "http://127.0.0.1:9877/cb"
_JWT_SECRET = "oidc-e2e-itest-secret"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture
def oidc_stack(ch_client, request):
    """Fake IdP (in-thread) + loom server (subprocess) wired to it.

    Params (via indirect parametrize): seed (pre-create the account, default
    True) and jit (enable JIT provisioning, default False); ports are
    kernel-assigned per instance. Yields (user_email, seeded_ns_or_None, loom_base).
    """
    params = getattr(request, "param", {})
    seed = params.get("seed", True)
    jit = params.get("jit", False)
    # Ports come from the kernel, never from a fixed list: under random test
    # order two default-param variants can run back to back, and the previous
    # fake IdP had not yet released 8097 when the next one tried to bind it.
    loom_port = params.get("loom_port") or free_port()
    base = f"http://127.0.0.1:{loom_port}"

    if os.environ.get("LOOM_CH_SECURE", "").lower() == "true":
        pytest.skip("OIDC e2e seeds a local namespaces row; skip against Cloud CH")

    org = unique_org("oidc")
    user_email = f"oidc-{uuid.uuid4().hex[:6]}@test.com"
    ns = f"oidc-{uuid.uuid4().hex[:6]}"
    if seed:
        now = _dt.datetime.now(_dt.UTC).replace(tzinfo=None, microsecond=0)
        # SSO-only account: empty password_hash, contact_email = the IdP email.
        ch_client.insert(
            "loom.namespaces",
            [
                [
                    org,
                    ns,
                    user_email,
                    user_email,
                    "",
                    "oidc e2e",
                    [],
                    True,
                    False,
                    None,
                    0,
                    0,
                    "private",
                    now,
                    now,
                ]
            ],
            column_names=[
                "org",
                "namespace",
                "owner_email",
                "contact_email",
                "password_hash",
                "description",
                "tags",
                "active",
                "auto_promote_enabled",
                "conflict_threshold",
                "retention_days",
                "warm_days",
                "visibility",
                "created_at",
                "updated_at",
            ],
        )

    idp = FakeIdP(user_email)
    env = {
        "LOOM_OAUTH_ENABLED": "true",
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_OAUTH_LOGIN_PROVIDER": "oidc",
        "LOOM_OIDC_ISSUER": idp.url,
        "LOOM_OIDC_CLIENT_ID": CLIENT_ID,
        "LOOM_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "LOOM_OAUTH_ISSUER": base,
        "LOOM_JWT_SECRET": _JWT_SECRET,
    }
    if jit:
        env.update(LOOM_OIDC_JIT_PROVISION="true", LOOM_OIDC_JIT_ORG=org)
    with idp.running(), loom_server("oidc", env, port=loom_port) as served:
        assert served == base
        yield user_email, (ns if seed else None), base


def _drive_brokered_login(base: str) -> dict:
    """Register a client + run the full brokered flow; return the loom token."""
    md = httpx.get(f"{base}/.well-known/oauth-authorization-server").json()
    cid = httpx.post(
        md["registration_endpoint"],
        json={"redirect_uris": [_MCP_REDIRECT], "client_name": "OIDC-E2E"},
    ).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())

    # authorize → loom redirects to the IdP (no password form)
    r = httpx.get(
        md["authorization_endpoint"],
        params={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": _MCP_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "mcp-xyz",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text[:300]
    # IdP auto-approves → redirects to loom's callback
    r2 = httpx.get(r.headers["location"], follow_redirects=False)
    assert r2.status_code == 302 and r2.headers["location"].startswith(f"{base}/oauth/callback")
    # loom callback: exchange + verify + map/provision → redirect to MCP client
    r3 = httpx.get(r2.headers["location"], follow_redirects=False)
    assert r3.status_code == 302, r3.text[:300]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r3.headers["location"]).query)
    assert r3.headers["location"].startswith(_MCP_REDIRECT)
    assert q["state"] == ["mcp-xyz"]
    code = q["code"][0]
    # token (loom code + PKCE → loom JWT)
    return httpx.post(
        md["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _MCP_REDIRECT,
            "client_id": cid,
            "code_verifier": verifier,
        },
    ).json()


def test_full_oidc_brokered_login(oidc_stack) -> None:
    user_email, ns, base = oidc_stack
    tok = _drive_brokered_login(base)
    assert tok["token_type"] == "Bearer" and tok["access_token"]
    who = httpx.get(
        f"{base}/v1/whoami", headers={"Authorization": f"Bearer {tok['access_token']}"}
    ).json()
    assert who["authenticated"] is True
    assert who["subject"] == user_email
    assert ns in (who.get("namespaces") or [])


@pytest.mark.parametrize(
    "oidc_stack",
    [{"seed": False, "jit": True}],
    indirect=True,
)
def test_jit_provisions_unknown_email(oidc_stack) -> None:
    """No pre-existing account + JIT on → first login auto-creates one."""
    user_email, ns, base = oidc_stack
    assert ns is None  # nothing seeded
    tok = _drive_brokered_login(base)
    assert tok["token_type"] == "Bearer" and tok["access_token"]
    who = httpx.get(
        f"{base}/v1/whoami", headers={"Authorization": f"Bearer {tok['access_token']}"}
    ).json()
    assert who["authenticated"] is True
    # Auto-provisioned a personal namespace the user owns, in the JIT org.
    assert who["subject"].startswith("u-")
    assert who["subject"] in (who.get("namespaces") or [])


def test_callback_rejects_unknown_state(oidc_stack) -> None:
    """A callback with a state that was never issued (forged/replayed) is
    refused — the pending record is the CSRF guard. (Unknown-email rejection is
    covered by the unit test on resolve_identity_for_email.)"""
    _user_email, _ns, base = oidc_stack
    bad = httpx.get(
        f"{base}/oauth/callback",
        params={"code": "x", "state": "never-issued"},
        follow_redirects=False,
    )
    assert bad.status_code == 400
