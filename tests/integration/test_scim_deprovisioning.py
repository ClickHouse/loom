"""SCIM deprovisioning end to end: an identity provider deactivates a person
over /scim/v2, and their session, the service account they made and their SSO
sign-in all stop; reactivating lets them sign in again, old tokens still dead.

Runs a Loom server against a fake OIDC provider, as test_cli_login_flow does.
"""

from __future__ import annotations

import datetime as _dt
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.auth.revocation import SCIM_PREFIX  # noqa: E402
from tests.integration._fake_idp import CLIENT_ID, CLIENT_SECRET, FakeIdP  # noqa: E402
from tests.integration._ports import free_port  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402
from tests.integration._spawn import loom_server  # noqa: E402

_JWT_SECRET = "scim-itest-secret-" + "0" * 32
_SCIM_TOKEN = secrets.token_hex(32)
_SCIM = {"Authorization": f"Bearer {_SCIM_TOKEN}", "Content-Type": "application/scim+json"}


def _seed(ch_client: Any, org: str, email: str) -> str:
    """An SSO-only account (no password) and its owner's admin grant."""
    ns = f"scim-{uuid.uuid4().hex[:6]}"
    now = _dt.datetime.now(_dt.UTC).replace(tzinfo=None, microsecond=0)
    ch_client.insert(
        "loom.namespaces",
        [[org, ns, email, email, "", "scim e2e", [], True, "private", now, now]],
        column_names=[
            "org",
            "namespace",
            "owner_email",
            "contact_email",
            "password_hash",
            "description",
            "tags",
            "active",
            "visibility",
            "created_at",
            "updated_at",
        ],
    )
    ch_client.insert(
        "loom.namespace_grants",
        [[org, ns, email, "admin", now, email, None, time.time_ns()]],
        column_names=[
            "org",
            "namespace",
            "subject",
            "role",
            "granted_at",
            "granted_by",
            "revoked_at",
            "version",
        ],
    )
    return ns


@pytest.fixture
def scim_server(ch_client) -> Iterator[tuple[str, str, str, str, Any]]:
    if os.environ.get("LOOM_CH_SECURE", "").lower() == "true":
        pytest.skip("seeds local namespaces rows; skip against Cloud CH")
    org = unique_org("scim")
    email = f"scim-{uuid.uuid4().hex[:6]}@corp.example"
    ns = _seed(ch_client, org, email)
    idp = FakeIdP(email)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_JWT_SECRET": _JWT_SECRET,
        "LOOM_OAUTH_LOGIN_PROVIDER": "oidc",
        "LOOM_OAUTH_ISSUER": base,
        "LOOM_OIDC_ISSUER": idp.url,
        "LOOM_OIDC_CLIENT_ID": CLIENT_ID,
        "LOOM_OIDC_CLIENT_SECRET": CLIENT_SECRET,
        "LOOM_SCIM_TOKEN": _SCIM_TOKEN,
    }
    try:
        with idp.running(), loom_server("scim", env, port=port):
            yield base, org, email, ns, ch_client
    finally:
        _cleanup(ch_client, email)


def _cleanup(ch_client: Any, email: str) -> None:
    # None of these rows names the org the suite purges by.
    for table, column, value in (
        ("scim_users", "email", email),
        ("revoked_tokens", "jti", email),
        ("revoked_tokens", "jti", SCIM_PREFIX + email),
    ):
        ch_client.command(
            f"ALTER TABLE loom.{table} DELETE WHERE {column} = %(e)s SETTINGS mutations_sync = 2",
            parameters={"e": value},
        )


def _sign_in(base: str) -> httpx.Response:
    """The dashboard's SSO sign-in, through the fake provider and back."""
    with httpx.Client() as browser:
        to_idp = browser.get(f"{base}/oauth/sso")
        return browser.get(browser.get(to_idp.headers["location"]).headers["location"])


def _token(back: httpx.Response) -> str:
    assert back.status_code == 302, back.text[:300]
    return back.headers["location"].split("#token=", 1)[1]


def _authenticated(base: str, token: str) -> bool:
    r = httpx.get(f"{base}/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    return bool(r.json().get("authenticated"))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _listed(base: str, token: str) -> dict[str, Any]:
    """The caller's Tokens list, by name."""
    r = httpx.get(f"{base}/v1/me/service_accounts", headers=_bearer(token))
    assert r.status_code == 200, r.text
    return {row["name"]: row for row in r.json()["rows"]}


def _org_admin_token(org: str, email: str) -> str:
    """An org admin's token, signed in a process of its own under the server's secret."""
    mint = (
        "import sys; from server.auth import keyring; from server.auth.jwt import mint_token; "
        "keyring.load_for_offline_mint(); "
        "print(mint_token(sys.argv[1], sys.argv[2], ['*'], ['org_admin'], is_service_account=False))"
    )
    minted = subprocess.run(
        [sys.executable, "-c", mint, email, org],
        env={**os.environ, "LOOM_JWT_SECRET": _JWT_SECRET},
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    return minted.stdout.strip().splitlines()[-1]


def _set_active(base: str, uid: str, active: bool) -> dict[str, Any]:
    r = httpx.patch(
        f"{base}/scim/v2/Users/{uid}",
        headers=_SCIM,
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "value": {"active": active}}],
        },
        timeout=30,  # a reactivation answers once every replica's clock is past its cutoff
    )
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def test_deactivating_over_scim_ends_the_person_and_reactivating_lets_them_back(
    scim_server,
) -> None:
    base, org, email, ns, ch_client = scim_server
    session = _token(_sign_in(base))
    made = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers=_bearer(session),
        json={"name": "ci-bot", "namespaces": [ns], "role": "viewer"},
    )
    assert made.status_code == 200, made.text
    service_account = made.json()["token"]
    never = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers=_bearer(session),
        json={"name": "never", "namespaces": [ns], "role": "viewer", "ttl_days": 0},
    )
    assert never.status_code == 200 and never.json()["expires_at"] is None, never.text
    mcp = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers=_bearer(session),
        json={"ttl_days": 0},
    )
    assert mcp.status_code == 200, mcp.text
    # A token the person's own token made: created by its `sa:` subject.
    parent = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers=_bearer(session),
        json={"name": "parent-bot", "namespaces": [ns], "role": "viewer"},
    )
    assert parent.status_code == 200, parent.text
    child = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers=_bearer(parent.json()["token"]),
        json={"name": "child-bot", "namespaces": [ns], "role": "viewer"},
    )
    assert child.status_code == 200, child.text
    made_by_person = [
        service_account,
        never.json()["token"],
        mcp.json()["token"],
        parent.json()["token"],
        child.json()["token"],
    ]
    # Another org admin's token, which the person's deactivation must leave alone.
    admin_email = f"admin-{uuid.uuid4().hex[:6]}@corp.example"
    admin_ns = _seed(ch_client, org, admin_email)
    admin = _org_admin_token(org, admin_email)
    minted = httpx.post(
        f"{base}/v1/admin/token.mint",
        headers=_bearer(admin),
        json={"name": "admin-bot", "org": org, "namespaces": [admin_ns], "roles": ["viewer"]},
    )
    assert minted.status_code == 200, minted.text
    admin_bot = minted.json()["token"]
    assert all(_authenticated(base, t) for t in [session, *made_by_person, admin_bot])
    # The address also names an account in another org now: sign-in would refuse
    # to pick one, and deactivation must still reach this one.
    _seed(ch_client, unique_org("scim"), email)

    assert httpx.get(f"{base}/scim/v2/Users").status_code == 401  # no token, no SCIM
    created = httpx.post(f"{base}/scim/v2/Users", headers=_SCIM, json={"userName": email})
    assert created.status_code == 201, created.text
    uid = created.json()["id"]
    found = httpx.get(
        f"{base}/scim/v2/Users", headers=_SCIM, params={"filter": f'userName eq "{email}"'}
    ).json()
    assert [u["id"] for u in found["Resources"]] == [uid]

    assert _set_active(base, uid, False)["active"] is False
    assert _set_active(base, uid, False)["active"] is False  # a retry changes nothing
    assert not any(_authenticated(base, t) for t in [session, *made_by_person])
    assert _authenticated(base, admin_bot)
    denied = _sign_in(base)
    assert denied.status_code == 403 and "deactivated" in denied.text

    assert _set_active(base, uid, True)["active"] is True
    ch_client.command(
        "ALTER TABLE loom.namespaces DELETE WHERE contact_email = %(e)s AND namespace != %(ns)s "
        "SETTINGS mutations_sync = 2",
        parameters={"e": email, "ns": ns},
    )
    again = _token(_sign_in(base))
    assert _authenticated(base, again)
    assert not any(_authenticated(base, t) for t in [session, *made_by_person])  # still dead
    listed = _listed(base, again)
    assert len(listed) == 5 and "child-bot" in listed  # the one parent-bot made too
    assert all(row["revoked_by"] == "scim" for row in listed.values())
    assert _authenticated(base, admin_bot)
    assert _listed(base, admin)["admin-bot"]["revoked_at"] is None
    # A dead token's name is free again.
    remade = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers=_bearer(again),
        json={"name": "never", "namespaces": [ns], "role": "viewer"},
    )
    assert remade.status_code == 200, remade.text
    got = httpx.get(f"{base}/scim/v2/Users/{uid}", headers=_SCIM).json()
    assert got["userName"] == email and got["active"] is True
