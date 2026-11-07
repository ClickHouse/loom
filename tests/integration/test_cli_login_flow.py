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
    # The owner's admin grant, as account creation writes it.
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
        try:
            line = self.printed.get(timeout=10)
        except queue.Empty:
            # The sign-in never printed its code: say why (a 429 from the
            # device-start limit, say), not that a queue stayed empty.
            raise AssertionError(f"loom login printed no code: {self.error!r}") from None
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
    """Sign in at the fake IdP (it signs in whoever `FakeIdP.email` names) in
    one browser, and return Loom's page after the callback."""
    with httpx.Client() as browser:
        to_idp = browser.post(f"{base}/oauth/device", data={"user_code": user_code})
        assert to_idp.status_code == 302, to_idp.text
        back = browser.get(to_idp.headers["location"])
        assert back.status_code == 302
        return browser.get(back.headers["location"])


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
    # Every test server here starts sign-ins from 127.0.0.1, and the limit on
    # them is durable: a suite run again within its window would be refused.
    ch_client.command("DELETE FROM loom.auth_attempts WHERE kind LIKE 'device.start%'")
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

    # The client goes on (its new token is used), then someone presents the old
    # one they kept: refused, and the session ends.
    credentials.access_token(refused=second["access_token"])
    third = _stored()
    assert _refresh(base, first["refresh_token"]).json()["error"] == "invalid_grant"
    assert _refresh(base, third["refresh_token"]).json()["error"] == "invalid_grant"
    assert _whoami(base, third["access_token"])["authenticated"] is False


def test_logout_revokes_the_session_on_the_server(password_server) -> None:
    base, _org, _ns, email, password = password_server
    login = _Login(base)
    _approve_with_password(base, login.user_code(), email, password)
    login.wait()
    entry = _stored()
    assert credentials.logout() is True
    assert _whoami(base, entry["access_token"])["authenticated"] is False
    assert _refresh(base, entry["refresh_token"]).json()["error"] == "invalid_grant"
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
    from server.auth.passwords import hash_password

    org = unique_org("sso")
    email = f"sso-{uuid.uuid4().hex[:6]}@corp.example"
    _seed(ch_client, org, email, "")  # an SSO-only account
    # Someone self-signed up with this email before SSO: never verified, never inherited.
    _seed(ch_client, org, email, hash_password("squatter"))
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
    page = httpx.get(f"{base}/")
    if page.status_code == 200:  # the dashboard is built
        assert "window.__LOOM_SSO = true" in page.text
    password = httpx.post(f"{base}/v1/auth/login", json={"identifier": email, "password": "x"})
    assert password.status_code == 404
    with httpx.Client() as browser:
        to_idp = browser.get(f"{base}/oauth/sso")
        back = browser.get(browser.get(to_idp.headers["location"]).headers["location"])
    assert back.status_code == 302 and back.headers["location"].startswith("/me#token=")
    token = back.headers["location"].split("#token=", 1)[1]
    assert _whoami(base, token)["subject"] == email


def test_cli_login_through_the_identity_provider(sso_server) -> None:
    base, email, _idp = sso_server
    login = _Login(base)
    code = login.user_code()
    _approve(_approve_with_sso(base, code), base, code)
    who = login.wait()
    assert who["subject"] == email and len(who["namespaces"]) == 1
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


# ── an agent that never opens the dashboard ───────────────────────────────


def _session_for(base: str, home: Path, email: str, password: str, monkeypatch: Any) -> None:
    """Sign `email` in with `loom.login()`; its session lands in `home`'s ~/.loom."""
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", home / ".loom" / "credentials")
    login = _Login(base)
    _approve_with_password(base, login.user_code(), email, password)
    login.wait()


def _loom(home: Path, *args: str) -> Any:
    """`loom …` in a shell whose HOME holds one user's session; its JSON answer."""
    env = {**os.environ, "HOME": str(home), "LOOM_ENV_FILE": ""}
    env.pop("LOOM_API_KEY", None)
    # The child runs `loom`, whose session commands are this checkout's SDK; the
    # server environment CI installs does not carry it.
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(ROOT), str(ROOT / "sdk" / "python"), os.environ.get("PYTHONPATH")])
    )
    done = subprocess.run(
        [sys.executable, "-m", "server.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout) if done.stdout.strip() else None


def test_joining_a_namespace_is_requested_and_approved_from_the_cli(
    password_server, ch_client, tmp_path, monkeypatch
) -> None:
    from server.auth.passwords import hash_password

    base, org, owner_ns, owner, owner_password = password_server
    member = f"member-{uuid.uuid4().hex[:6]}@test.com"
    _seed(ch_client, org, member, hash_password("member-pw"))
    owner_home, member_home = tmp_path / "owner", tmp_path / "member"
    _session_for(base, owner_home, owner, owner_password, monkeypatch)
    _session_for(base, member_home, member, "member-pw", monkeypatch)

    _loom(member_home, "call", "namespace.join", f"namespace={owner_ns}", "role=writer")
    pending = _loom(owner_home, "call", "grants.pending_for_me")
    assert member in json.dumps(pending)
    _loom(
        owner_home,
        "call",
        "grants.approve",
        f"namespace={owner_ns}",
        f"subject={member}",
        "decision=approve",
    )
    _loom(
        member_home,
        "call",
        "memory.set",
        "memory_key=joined/fact/1",
        "value=written after approval",
        "--namespace",
        owner_ns,
    )
    got = _loom(owner_home, "call", "memory.get", "memory_key=joined/fact/1")
    assert got["value"] == "written after approval"


def test_service_accounts_and_account_views_need_no_dashboard(
    password_server, tmp_path, monkeypatch
) -> None:
    base, org, ns, email, password = password_server
    home = tmp_path / "owner"
    _session_for(base, home, email, password, monkeypatch)
    created = _loom(
        home,
        "call",
        "/v1/me/service_accounts.create",
        "--data",
        json.dumps({"name": "ci-bot", "namespaces": [ns], "role": "viewer"}),
    )
    assert _whoami(base, created["token"])["is_service_account"] is True
    # Minted from an hour-long session token, it lives as long as it asked
    # (the 90-day default), not the token's hour.
    assert 89 * 86400 < _whoami(base, created["token"])["exp"] - time.time() <= 90 * 86400
    forever = _loom(
        home,
        "call",
        "/v1/me/service_accounts.create",
        "--data",
        json.dumps({"name": "forever", "namespaces": [ns], "ttl_days": 0}),
    )
    assert _whoami(base, forever["token"]).get("exp") is None
    listed = _loom(home, "call", "/v1/me/service_accounts")
    rows = {r["name"]: r for r in listed["rows"]}
    assert set(rows) == {"ci-bot", "forever"}
    assert rows["forever"]["ttl_days"] == 0 and rows["forever"]["expires_at"] is None
    _loom(home, "call", "/v1/me/service_accounts.revoke", "name=forever")
    assert _whoami(base, forever["token"])["authenticated"] is False
    # A live token's name is not taken again: the new row would replace its
    # row, leaving the live token listed nowhere.
    session = json.loads((home / ".loom" / "credentials").read_text())["profiles"]["default"]
    again = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={"name": "ci-bot", "namespaces": [ns]},
    )
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "TOKEN_NAME_TAKEN"
    assert "your token 'ci-bot' is live" in again.json()["detail"]["message"]
    # An agent's own rows are named `agent · <name>`; a person's token cannot be.
    agentlike = httpx.post(
        f"{base}/v1/me/service_accounts.create",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        json={"name": "agent · fleet", "namespaces": [ns]},
    )
    assert agentlike.status_code == 422, agentlike.text
    # A revoked one's name is free again.
    assert _loom(
        home,
        "call",
        "/v1/me/service_accounts.create",
        "--data",
        json.dumps({"name": "forever", "namespaces": [ns]}),
    )["token"]
    _loom(home, "call", "/v1/me/service_accounts.revoke", "name=ci-bot")
    assert _whoami(base, created["token"])["authenticated"] is False
    assert "totals" in _loom(home, "call", "/v1/me/sharing-summary")
    assert _loom(home, "call", "/v1/health")["status"]

    async def through_the_sdk() -> list[dict[str, Any]]:
        agent = Agent(org, ns, "account-e2e", base_url=base)
        try:
            await agent.account.create_token([ns], name="sdk-bot")
            return await agent.account.tokens()
        finally:
            await agent.close()

    rows = asyncio.run(through_the_sdk())
    assert "sdk-bot" in [r["name"] for r in rows]

    # A machine token outlives the session it was made from.
    kept = _loom(
        home,
        "call",
        "/v1/me/service_accounts.create",
        "name=kept",
        f"namespaces:={json.dumps([ns])}",
    )
    credentials.logout()
    assert _whoami(base, kept["token"])["authenticated"] is True


def test_a_replayed_session_ends_and_spares_the_rest(
    password_server, tmp_path, monkeypatch
) -> None:
    base, _org, ns, email, password = password_server
    laptop, desktop = tmp_path / "laptop", tmp_path / "desktop"
    _session_for(base, desktop, email, password, monkeypatch)
    other = _loom(
        desktop,
        "call",
        "/v1/me/service_accounts.create",
        "name=desk",
        f"namespaces:={json.dumps([ns])}",
    )
    _session_for(base, laptop, email, password, monkeypatch)
    made = _loom(
        laptop,
        "call",
        "/v1/me/service_accounts.create",
        "name=ci",
        f"namespaces:={json.dumps([ns])}",
    )
    first = _stored()
    credentials.access_token(refused=first["access_token"])  # the client goes on...
    credentials.access_token(refused=_stored()["access_token"])
    # ...and a copy of the first token turns up: this session ends. The CI
    # token minted from it is its owner's to revoke.
    assert _refresh(base, first["refresh_token"]).json()["error"] == "invalid_grant"
    assert _whoami(base, _stored()["access_token"])["authenticated"] is False
    assert _whoami(base, made["token"])["authenticated"] is True
    # The same person's other session, and what it minted, are untouched.
    assert _whoami(base, other["token"])["authenticated"] is True
    assert _loom(desktop, "call", "/v1/me/sharing-summary")["subject"] == email


def test_a_lost_refresh_answer_costs_a_sign_in_and_nothing_else(
    password_server, tmp_path, monkeypatch
) -> None:
    base, _org, ns, email, password = password_server
    home = tmp_path / "laptop"
    _session_for(base, home, email, password, monkeypatch)
    made = _loom(
        home, "call", "/v1/me/service_accounts.create", "name=ci", f"namespaces:={json.dumps([ns])}"
    )
    first = _stored()
    assert _refresh(base, first["refresh_token"]).status_code == 200  # the answer is lost
    with pytest.raises(AuthInvalid, match="run `loom login`"):
        credentials.access_token(refused=first["access_token"])
    assert _whoami(base, made["token"])["authenticated"] is True
