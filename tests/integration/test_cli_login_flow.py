"""`loom login` end to end: the SDK's device-grant client against a real
server, approved through the real approval page, with the password form and
with an OIDC identity provider (the fake one in `_fake_idp`).

Covers: password sign-in and the SDK picking the session up; SSO sign-in for
the dashboard and the CLI; JIT accounts and a refused email domain; refresh
rotation and a replayed refresh token ending the session; logout; a user the
identity provider deactivates losing Loom at the next refresh; and a CI token
minted with no browser.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import queue
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from loom import Agent, credentials  # noqa: E402
from loom.errors import AuthInvalid  # noqa: E402

from tests.integration._fake_idp import CLIENT_ID, CLIENT_SECRET, FakeIdP  # noqa: E402
from tests.integration._ports import free_port  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402
from tests.integration._spawn import loom_server  # noqa: E402

pytestmark = pytest.mark.slow

_JWT_SECRET = "cli-login-itest-secret"
_COLUMNS = [
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
]


def _seed(ch_client: Any, org: str, email: str, password_hash: str) -> str:
    ns = f"login-{uuid.uuid4().hex[:6]}"
    now = _dt.datetime.now(_dt.UTC).replace(tzinfo=None, microsecond=0)
    row = [org, ns, email, email, password_hash, "login e2e", [], True, False, None, 0, 0]
    ch_client.insert("loom.namespaces", [[*row, "private", now, now]], column_names=_COLUMNS)
    return ns


@pytest.fixture(autouse=True)
def _credentials_file(tmp_path, monkeypatch):
    if os.environ.get("LOOM_CH_SECURE", "").lower() == "true":
        pytest.skip("seeds local namespaces rows; skip against Cloud CH")
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", tmp_path / "credentials")
    for var in ("LOOM_PROFILE", "LOOM_BASE_URL", "LOOM_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class _Login:
    """`loom.login()` on a thread, as a user's terminal would run it."""

    def __init__(self, base: str) -> None:
        self.printed: queue.Queue[str] = queue.Queue()
        self.result: dict[str, Any] = {}
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(base,), daemon=True)
        self._thread.start()

    def _run(self, base: str) -> None:
        try:
            self.result = credentials.login(base, open_browser=False, echo=self.printed.put)
        except BaseException as e:  # noqa: BLE001 — handed to the test thread to assert on
            self.error = e

    def user_code(self) -> str:
        line = self.printed.get(timeout=10)
        match = re.search(r"enter the code ([A-Z]{4}-[A-Z]{4})", line)
        assert match, line
        return match.group(1)

    def wait(self) -> dict[str, Any]:
        self._thread.join(timeout=60)
        assert not self._thread.is_alive(), "loom login did not finish"
        if self.error is not None:
            raise self.error
        return self.result


def _approve(page: httpx.Response, base: str, user_code: str) -> None:
    assert page.status_code == 200, page.text
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.text)
    assert ticket, page.text
    r = httpx.post(
        f"{base}/oauth/device/approve",
        data={"user_code": user_code, "ticket": ticket.group(1), "decision": "approve"},
    )
    assert r.status_code == 200 and "signed in" in r.text


def _approve_with_password(base: str, user_code: str, email: str, password: str) -> None:
    page = httpx.post(
        f"{base}/oauth/device",
        data={"user_code": user_code, "identifier": email, "password": password},
    )
    _approve(page, base, user_code)


def _approve_with_sso(base: str, user_code: str) -> httpx.Response:
    """Sign in at the fake IdP (it signs in whoever `FakeIdP.email` names)
    and return Loom's page after the callback."""
    to_idp = httpx.post(f"{base}/oauth/device", data={"user_code": user_code})
    assert to_idp.status_code == 302, to_idp.text
    back = httpx.get(to_idp.headers["location"])
    assert back.status_code == 302
    return httpx.get(back.headers["location"])


def _whoami(base: str, token: str) -> dict[str, Any]:
    r = httpx.get(f"{base}/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    out: dict[str, Any] = r.json()
    return out


def _refresh(base: str, token: str) -> httpx.Response:
    return httpx.post(
        f"{base}/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": token, "client_id": "loom-cli"},
    )


def _stored() -> dict[str, Any]:
    entry: dict[str, Any] = json.loads(credentials.CREDENTIALS_FILE.read_text())["profiles"][
        "default"
    ]
    return entry


# ── password sign-in: a fresh install with no identity provider ───────────


@pytest.fixture
def password_server(ch_client) -> Iterator[tuple[str, str, str, str, str]]:
    from server.auth.passwords import hash_password

    org = unique_org("login")
    email = f"login-{uuid.uuid4().hex[:6]}@test.com"
    password = "pw-" + uuid.uuid4().hex[:8]
    ns = _seed(ch_client, org, email, hash_password(password))
    env = {"LOOM_AUTH_REQUIRED": "true", "LOOM_JWT_SECRET": _JWT_SECRET}
    with loom_server("login", env) as base:
        yield base, org, ns, email, password


def test_password_login_and_the_sdk_uses_the_stored_session(password_server) -> None:
    base, org, ns, email, password = password_server
    login = _Login(base)
    _approve_with_password(base, login.user_code(), email, password)
    who = login.wait()
    assert (who["subject"], who["org"]) == (email, org)

    async def use_it() -> Any:
        agent = Agent(org, ns, "login-e2e", base_url=base)  # the session is that server's
        try:
            await agent.memory.set("login/fact/1", "the stored session works")
            return await agent.memory.get("login/fact/1")
        finally:
            await agent.close()

    got = asyncio.run(use_it())
    assert "the stored session works" in str(got)


def test_refresh_rotates_and_a_replayed_token_ends_the_session(password_server) -> None:
    base, _org, _ns, email, password = password_server
    login = _Login(base)
    _approve_with_password(base, login.user_code(), email, password)
    login.wait()
    first = _stored()
    credentials.access_token(refused=first["access_token"])  # force one rotation
    second = _stored()
    assert second["refresh_token"] != first["refresh_token"]
    assert _whoami(base, second["access_token"])["authenticated"] is True

    # Someone kept the old refresh token and uses it: refused, and the session ends.
    assert _refresh(base, first["refresh_token"]).json()["detail"]["error"] == "invalid_grant"
    assert _refresh(base, second["refresh_token"]).json()["detail"]["error"] == "invalid_grant"
    assert _whoami(base, second["access_token"])["authenticated"] is False


def test_logout_revokes_the_session_on_the_server(password_server) -> None:
    base, _org, _ns, email, password = password_server
    login = _Login(base)
    _approve_with_password(base, login.user_code(), email, password)
    login.wait()
    entry = _stored()
    assert credentials.logout() is True
    assert _whoami(base, entry["access_token"])["authenticated"] is False
    assert _refresh(base, entry["refresh_token"]).json()["detail"]["error"] == "invalid_grant"
    assert json.loads(credentials.CREDENTIALS_FILE.read_text())["profiles"] == {}


def test_a_ci_job_gets_a_scoped_token_with_no_browser(password_server) -> None:
    base, org, ns, _email, _password = password_server
    minted = subprocess.run(
        [
            sys.executable,
            "-m",
            "server.cli",
            "mint-token",
            "--subject",
            "sa:ci-e2e",
            "--org",
            org,
            "--service-account",
            "--namespace-role",
            f"{ns}=viewer",
            "--ttl-days",
            "1",
        ],
        env={**os.environ, "LOOM_JWT_SECRET": _JWT_SECRET},
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    token = minted.stdout.strip().splitlines()[-1]
    who = _whoami(base, token)
    assert who["authenticated"] is True and who["is_service_account"] is True
    assert who["namespaces"] == [ns]


# ── single sign-on through an OIDC identity provider ──────────────────────


@pytest.fixture
def sso_server(ch_client) -> Iterator[tuple[str, str, FakeIdP]]:
    org = unique_org("sso")
    email = f"sso-{uuid.uuid4().hex[:6]}@corp.example"
    _seed(ch_client, org, email, "")  # an SSO-only account
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
        "LOOM_OIDC_JIT_PROVISION": "true",
        "LOOM_OIDC_JIT_ORG": org,
        "LOOM_OIDC_JIT_EMAIL_DOMAINS": "corp.example",
    }
    with idp.running(), loom_server("sso", env, port=port):
        yield base, email, idp


def test_dashboard_sign_in_goes_straight_to_the_identity_provider(sso_server) -> None:
    base, email, _idp = sso_server
    page = httpx.get(f"{base}/me")
    if page.status_code == 200:  # the dashboard is built
        assert "window.__LOOM_SSO = true" in page.text
    password = httpx.post(f"{base}/v1/auth/login", json={"identifier": email, "password": "x"})
    assert password.status_code == 404
    to_idp = httpx.get(f"{base}/oauth/sso")
    back = httpx.get(httpx.get(to_idp.headers["location"]).headers["location"])
    assert back.status_code == 302 and back.headers["location"].startswith("/me#token=")
    token = back.headers["location"].split("#token=", 1)[1]
    assert _whoami(base, token)["subject"] == email


def test_cli_login_through_the_identity_provider(sso_server) -> None:
    base, email, _idp = sso_server
    login = _Login(base)
    code = login.user_code()
    _approve(_approve_with_sso(base, code), base, code)
    assert login.wait()["subject"] == email
    assert _stored()["refresh_token"]


def test_a_first_sign_in_from_an_allowed_domain_creates_the_account(sso_server) -> None:
    base, _email, idp = sso_server
    idp.email = f"new-{uuid.uuid4().hex[:6]}@corp.example"
    login = _Login(base)
    code = login.user_code()
    _approve(_approve_with_sso(base, code), base, code)
    who = login.wait()
    assert who["subject"].startswith("u-new-") and who["subject"] in who["namespaces"]


def test_a_sign_in_from_another_domain_is_denied(sso_server) -> None:
    base, _email, idp = sso_server
    idp.email = f"outsider-{uuid.uuid4().hex[:6]}@elsewhere.example"
    login = _Login(base)
    page = _approve_with_sso(base, login.user_code())
    assert page.status_code == 403
    with pytest.raises(AuthInvalid, match="denied"):
        login.wait()


def test_a_user_deactivated_at_the_identity_provider_is_cut_off_at_refresh(sso_server) -> None:
    base, email, idp = sso_server
    login = _Login(base)
    code = login.user_code()
    _approve(_approve_with_sso(base, code), base, code)
    login.wait()
    entry = _stored()
    credentials.access_token(refused=entry["access_token"])  # still active: refreshes
    idp.deactivate(email)
    with pytest.raises(AuthInvalid, match="ended"):
        credentials.access_token(refused=_stored()["access_token"])
    assert _whoami(base, entry["access_token"])["authenticated"] is False


def test_sso_logout_also_revokes_the_identity_provider_token(sso_server) -> None:
    base, _email, idp = sso_server
    login = _Login(base)
    code = login.user_code()
    _approve(_approve_with_sso(base, code), base, code)
    login.wait()
    assert credentials.logout() is True
    assert idp.revoked, "loom did not revoke the identity provider's refresh token"
