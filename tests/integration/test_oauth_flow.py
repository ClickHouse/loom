"""End-to-end OAuth 2.1 one-click-connect handshake.

Spawns a separate loom server with LOOM_OAUTH_ENABLED=true + auth on, seeds a
login-able user, and drives the FULL handshake a real MCP client performs:
discovery → dynamic registration → authorize (login+consent) → token (PKCE) →
use the access token on a real op (whoami) → refresh rotation → PKCE reject.

This closes the boundary the unit tests can't: that the issued token is a real
loom JWT the server accepts, minted subject-aligned to the user.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import os
import secrets
import sys
import urllib.parse
import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration._resources import unique_org
from tests.integration._spawn import loom_server

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

_REDIRECT = "http://127.0.0.1:9876/cb"
_JWT_SECRET = "oauth-e2e-itest-secret"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


@pytest.fixture
def oauth_server(ch_client):
    """A loom server with OAuth enabled + a seeded login-able user.

    Separate process/port so it doesn't disturb the suite-wide server. Inherits
    LOOM_CH_* from the env (same CH the ch_client fixture seeds into).
    """
    if os.environ.get("LOOM_CH_SECURE", "").lower() == "true":
        pytest.skip("OAuth e2e seeds a local namespaces row; skip against Cloud CH")

    from server.auth.passwords import hash_password

    org = unique_org("oauth")
    email = f"oauth-e2e-{uuid.uuid4().hex[:6]}@test.com"
    ns = f"oauth-e2e-{uuid.uuid4().hex[:6]}"
    password = "hunter2-" + uuid.uuid4().hex[:8]
    now = _dt.datetime.now(_dt.UTC).replace(tzinfo=None, microsecond=0)
    ch_client.insert(
        "loom.namespaces",
        [
            [
                org,
                ns,
                email,
                email,
                hash_password(password),
                "oauth e2e",
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

    env = {
        "LOOM_OAUTH_ENABLED": "true",
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_JWT_SECRET": _JWT_SECRET,
    }
    with loom_server("oauth", env) as base:
        yield base, email, password, ns


def test_full_oauth_handshake(oauth_server) -> None:
    base, email, password, ns = oauth_server

    # 1. discovery
    md = httpx.get(f"{base}/.well-known/oauth-authorization-server").json()
    assert md["code_challenge_methods_supported"] == ["S256"]
    prm = httpx.get(f"{base}/.well-known/oauth-protected-resource").json()
    assert prm["authorization_servers"] == [md["issuer"]]

    # 2. dynamic client registration
    cid = httpx.post(
        md["registration_endpoint"],
        json={"redirect_uris": [_REDIRECT], "client_name": "E2E"},
    ).json()["client_id"]

    # 3. PKCE + 4. authorize (login + consent → code)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    r = httpx.post(
        md["authorization_endpoint"],
        data={
            "client_id": cid,
            "redirect_uri": _REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "identifier": email,
            "password": password,
            "state": "xyz",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text[:300]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    code = q["code"][0]

    # 5. token (code + PKCE → loom JWT)
    tok = httpx.post(
        md["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT,
            "client_id": cid,
            "code_verifier": verifier,
        },
    ).json()
    assert tok["token_type"] == "Bearer" and tok["access_token"] and tok["refresh_token"]

    # 6. the real proof: the issued token authenticates a real op, subject-aligned
    who = httpx.get(
        f"{base}/v1/whoami", headers={"Authorization": f"Bearer {tok['access_token']}"}
    ).json()
    assert who["authenticated"] is True
    assert who["subject"] == email
    assert ns in (who.get("namespaces") or [])  # subject-aligned → joins apply

    # 7. refresh rotates (old refresh token is single-use)
    tok2 = httpx.post(
        md["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": cid,
        },
    ).json()
    assert tok2["access_token"]
    reused = httpx.post(
        md["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": cid,
        },
    )
    assert reused.status_code == 400  # rotated out

    # 8. a wrong PKCE verifier is rejected (fresh code)
    cid2 = httpx.post(md["registration_endpoint"], json={"redirect_uris": [_REDIRECT]}).json()[
        "client_id"
    ]
    r2 = httpx.post(
        md["authorization_endpoint"],
        data={
            "client_id": cid2,
            "redirect_uri": _REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "identifier": email,
            "password": password,
        },
        follow_redirects=False,
    )
    code2 = urllib.parse.parse_qs(urllib.parse.urlparse(r2.headers["location"]).query)["code"][0]
    bad = httpx.post(
        md["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code2,
            "redirect_uri": _REDIRECT,
            "client_id": cid2,
            "code_verifier": "the-wrong-verifier",
        },
    )
    assert bad.status_code == 400
