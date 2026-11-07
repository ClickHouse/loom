"""The device grant behind `loom login`, session families, revocation, and the
SSO-only paths: every grant and every refusal, against the in-memory store."""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from server.auth import device, jit, login, oauth, oidc, revocation, scim, sso_links
from server.auth.jwt import mint_token
from server.auth.passwords import hash_password
from server.identity.app import password_link
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app
from tests.unit.security_headers import assert_security_headers

pytestmark = pytest.mark.usefixtures("auth_attempts")

_USER = ("alice@example.com", "acme", ["alice"], ["writer"])


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    """In-memory store, a loaded empty revocation list whose writes land
    locally, a recorded audit trail, and one password account."""
    store = MemoryOAuthStore()
    monkeypatch.setattr(oauth, "store", store)
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "password")
    monkeypatch.setattr(revocation, "_loaded", True)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    revoked: list[str] = []
    expiries: list[Any] = []

    async def revoke(token_id: str, **kw: Any) -> dict[str, Any]:
        revoked.append(token_id)
        expiries.append(kw.get("expires_at"))
        before = kw.get("issued_before")
        revocation._apply_locally(token_id, int(before.timestamp()) + 1 if before else None)
        return {}

    monkeypatch.setattr(revocation, "revoke", revoke)

    async def subjects_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return any(revocation.is_revoked(s, issued_at=issued_at) for s in subjects)

    monkeypatch.setattr(revocation, "subjects_revoked", subjects_revoked)
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, **kw: Any) -> str:
        events.append((event_type, kw))
        return ""

    monkeypatch.setattr(oauth.audit, "emit", emit)

    async def no_password_accounts(email: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(sso_links, "unlinked_accounts", no_password_accounts)

    async def no_links(email: str) -> list[tuple[str, str]]:
        return []

    monkeypatch.setattr(sso_links, "reaching", no_links)
    monkeypatch.setattr(device.audit, "emit", emit)

    async def credentials(identifier: str, password: str, *, address: str = "") -> Any:
        return _USER if (identifier, password) == ("alice@example.com", "pw") else None

    monkeypatch.setattr(login, "resolve_user_credentials", credentials)

    async def active(subject: str, org: str, roles: list[str]) -> list[str]:
        return roles

    monkeypatch.setattr(login, "refreshed_roles", active)
    monkeypatch.setattr(device, "_last_polls", OrderedDict())
    return {"store": store, "revoked": revoked, "expiries": expiries, "events": events}


def _client() -> TestClient:
    return TestClient(oauth_app())


def _start(c: TestClient) -> dict[str, Any]:
    r = c.post("/oauth/device_authorization", data={"client_id": device.DEVICE_CLIENT_ID})
    assert r.status_code == 200, r.text
    return r.json()


def _poll(c: TestClient, device_code: str) -> httpx.Response:
    device._last_polls.clear()  # each test poll stands for one a full interval later
    return c.post(
        "/oauth/token",
        data={
            "grant_type": device.DEVICE_GRANT,
            "device_code": device_code,
            "client_id": device.DEVICE_CLIENT_ID,
        },
    )


def _sign_in(c: TestClient, user_code: str, password: str = "pw") -> httpx.Response:
    return c.post(
        "/oauth/device",
        data={"user_code": user_code, "identifier": "alice@example.com", "password": password},
    )


def _ticket(page: str) -> str:
    match = re.search(r'name="ticket" value="([^"]+)"', page)
    assert match, page
    return match.group(1)


def _approve(c: TestClient, user_code: str, page: str, decision: str = "approve") -> httpx.Response:
    return c.post(
        "/oauth/device/approve",
        data={"user_code": user_code, "ticket": _ticket(page), "decision": decision},
    )


def _signed_in_tokens(c: TestClient) -> dict[str, Any]:
    grant = _start(c)
    page = _sign_in(c, grant["user_code"])
    assert _approve(c, grant["user_code"], page.text).status_code == 200
    r = _poll(c, grant["device_code"])
    assert r.status_code == 200, r.text
    return r.json()


def _refresh(c: TestClient, token: str) -> httpx.Response:
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": device.DEVICE_CLIENT_ID,
        },
    )


def _error(r: httpx.Response) -> str:
    return str(r.json()["error"])


# ── device authorization ──────────────────────────────────────────────────


def test_device_authorization_answers_rfc_8628_fields(_world) -> None:
    grant = _start(_client())
    assert re.fullmatch(r"[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}", grant["user_code"])
    assert grant["verification_uri"].endswith("/oauth/device")
    assert grant["verification_uri_complete"].endswith(f"?user_code={grant['user_code']}")
    assert (grant["expires_in"], grant["interval"]) == (600, 5)
    stored = next(iter(_world["store"].records("device").values()))
    # Neither code is stored as itself.
    assert grant["device_code"] not in str(stored)
    assert device.normalize_user_code(grant["user_code"]) not in str(stored)


def test_device_authorization_needs_the_cli_client_id() -> None:
    r = _client().post("/oauth/device_authorization", data={"client_id": "someone"})
    assert (r.status_code, _error(r)) == (400, "invalid_client")


def test_device_authorization_is_rate_limited_per_address() -> None:
    c = _client()
    for _ in range(30):
        _start(c)
    r = c.post("/oauth/device_authorization", data={"client_id": device.DEVICE_CLIENT_ID})
    assert r.status_code == 429


# ── polling ───────────────────────────────────────────────────────────────


def test_polling_before_approval_is_pending() -> None:
    c = _client()
    r = _poll(c, _start(c)["device_code"])
    assert (r.status_code, _error(r)) == (400, "authorization_pending")


def test_polling_faster_than_the_interval_is_slowed_down() -> None:
    c = _client()
    code = _start(c)["device_code"]
    _poll(c, code)
    r = c.post(
        "/oauth/token",
        data={"grant_type": device.DEVICE_GRANT, "device_code": code, "client_id": "loom-cli"},
    )
    assert _error(r) == "slow_down"


def test_an_unknown_device_code_is_an_invalid_grant() -> None:
    assert _error(_poll(_client(), "made-up")) == "invalid_grant"


def test_an_unknown_device_code_takes_no_poll_slot() -> None:
    assert _error(_poll(_client(), "made-up")) == "invalid_grant"
    assert not device._last_polls


def test_a_device_code_for_another_client_is_refused() -> None:
    c = _client()
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": device.DEVICE_GRANT,
            "device_code": _start(c)["device_code"],
            "client_id": "x",
        },
    )
    assert _error(r) == "invalid_client"


def test_an_expired_device_code_says_so(_world) -> None:
    c = _client()
    grant = _start(c)
    _world["store"].change("device", exp=int(time.time()) - 1)
    assert _error(_poll(c, grant["device_code"])) == "expired_token"


# ── the approval page ─────────────────────────────────────────────────────


def test_a_wrong_user_code_is_refused_and_counted() -> None:
    c = _client()
    for _ in range(10):
        assert _sign_in(c, "BBBB-BBBB").status_code == 400
    assert _sign_in(c, "BBBB-BBBB").status_code == 429


def test_a_code_approved_by_someone_else_tells_its_owner_to_log_out() -> None:
    # Whoever approves first decides; the terminal has already collected its
    # tokens when the owner gets to the page, and the owner is not locked out.
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sign_in(c, grant["user_code"]).text)
    assert _poll(c, grant["device_code"]).status_code == 200
    for _ in range(11):
        r = _sign_in(c, grant["user_code"])
        assert r.status_code == 400 and "already used" in r.text and "loom logout" in r.text


def test_a_denied_code_says_to_log_in_again() -> None:
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sign_in(c, grant["user_code"]).text, decision="deny")
    r = _sign_in(c, grant["user_code"])
    assert r.status_code == 400 and "loom login" in r.text and "loom logout" not in r.text


def test_a_wrong_password_keeps_the_grant_pending() -> None:
    c = _client()
    grant = _start(c)
    r = _sign_in(c, grant["user_code"], password="nope")
    assert r.status_code == 401 and "Invalid email or password" in r.text
    assert _error(_poll(c, grant["device_code"])) == "authorization_pending"


def test_signing_in_shows_a_confirm_page_that_cannot_be_framed() -> None:
    c = _client()
    grant = _start(c)
    page = _sign_in(c, grant["user_code"].lower().replace("-", " "))
    assert page.status_code == 200
    assert "alice@example.com" in page.text and grant["user_code"] in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    # Signed in is not approved.
    assert _error(_poll(c, grant["device_code"])) == "authorization_pending"


def test_the_server_headers_leave_the_sign_in_page_its_own_policies() -> None:
    from server.main import attach_browser_middleware

    app = oauth_app()
    attach_browser_middleware(app)
    page = TestClient(app, base_url="http://localhost").get("/oauth/device")
    assert page.status_code == 200
    # CSP is the only page header whose value differs from the middleware default.
    csp = oauth._PAGE_HEADERS["Content-Security-Policy"]
    assert page.headers.get_list("Content-Security-Policy") == [csp]
    assert_security_headers(page)


def test_approval_needs_the_ticket_from_the_signed_in_page() -> None:
    c = _client()
    grant = _start(c)
    _sign_in(c, grant["user_code"])
    forged = c.post(
        "/oauth/device/approve",
        data={"user_code": grant["user_code"], "ticket": "guess", "decision": "approve"},
    )
    assert forged.status_code == 400
    assert _error(_poll(c, grant["device_code"])) == "authorization_pending"


def test_a_ticket_approves_once(_world) -> None:
    c = _client()
    grant = _start(c)
    page = _sign_in(c, grant["user_code"]).text
    assert _approve(c, grant["user_code"], page).status_code == 200
    assert _approve(c, grant["user_code"], page).status_code == 400


def test_approved_grant_issues_a_session_and_is_audited(_world) -> None:
    tokens = _signed_in_tokens(_client())
    claims = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "alice@example.com" and claims["exp"] - claims["iat"] == 3600
    assert tokens["refresh_token"]
    approved = [kw for name, kw in _world["events"] if name == "auth.device_approved"]
    assert approved and approved[0]["subject"]["family"] == claims["sid"]


def test_denied_grant_ends_with_access_denied(_world) -> None:
    c = _client()
    grant = _start(c)
    page = _sign_in(c, grant["user_code"]).text
    _approve(c, grant["user_code"], page, decision="deny")
    assert _error(_poll(c, grant["device_code"])) == "access_denied"
    assert [n for n, _ in _world["events"]] == ["auth.device_denied"]


def test_a_replayed_device_code_ends_the_session(_world, monkeypatch) -> None:
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sign_in(c, grant["user_code"]).text)
    tokens = _poll(c, grant["device_code"]).json()
    assert _error(_poll(c, grant["device_code"])) == "invalid_grant"
    sid = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})["sid"]
    # The session; nothing else of the person's.
    assert _world["revoked"] == [sid]
    assert _error(_refresh(c, tokens["refresh_token"])) == "invalid_grant"


# ── refresh families ──────────────────────────────────────────────────────


def test_refresh_rotates_within_the_session() -> None:
    c = _client()
    first = _signed_in_tokens(c)
    second = _refresh(c, first["refresh_token"]).json()
    assert second["refresh_token"] != first["refresh_token"]
    claims = [
        pyjwt.decode(t["access_token"], options={"verify_signature": False})
        for t in (first, second)
    ]
    assert claims[0]["sid"] == claims[1]["sid"]  # one session
    assert claims[0]["jti"] != claims[1]["jti"]  # two tokens


def test_a_replayed_refresh_token_revokes_the_whole_family(_world, monkeypatch) -> None:
    c = _client()
    first = _signed_in_tokens(c)
    second = _refresh(c, first["refresh_token"]).json()
    _refresh(c, second["refresh_token"])
    # The old token comes back (someone kept a copy): refused, and the session ends.
    assert _error(_refresh(c, first["refresh_token"])) == "invalid_grant"
    sid = pyjwt.decode(second["access_token"], options={"verify_signature": False})["sid"]
    assert _world["revoked"] == [sid]
    # So the legitimate holder's session is dead too, access tokens and all.
    assert revocation.is_revoked(sid)
    reasons = [kw["subject"]["reason"] for n, kw in _world["events"] if n == "auth.session_revoked"]
    assert reasons == ["refresh token replayed"]


def test_a_refresh_past_its_deadline_answers_busy_and_keeps_the_token(monkeypatch) -> None:
    c = _client()
    token = _signed_in_tokens(c)["refresh_token"]

    async def stuck(subject: str, org: str, roles: list[str]) -> list[str]:
        await asyncio.sleep(5)
        return roles

    quick = login.refreshed_roles
    monkeypatch.setattr(oauth, "_REFRESH_DEADLINE_S", 0.2)
    monkeypatch.setattr(login, "refreshed_roles", stuck)
    r = _refresh(c, token)
    assert r.status_code == 503 and _error(r) == "temporarily_unavailable"
    monkeypatch.setattr(login, "refreshed_roles", quick)
    assert _refresh(c, token).status_code == 200


def test_a_failed_refresh_puts_its_token_back_even_when_cancelled(monkeypatch) -> None:
    # The rollback of a failed refresh runs to its end: a request cancelled
    # in it must not leave the token spent with nothing issued in its place.
    from server.core import shielded

    c = _client()
    token = _signed_in_tokens(c)["refresh_token"]
    original_save, original_session = oauth.store.save_refresh, oauth._refresh_session
    started, go = asyncio.Event(), asyncio.Event()

    async def slow_save(t: str, g: Any) -> None:
        started.set()
        await go.wait()
        await original_save(t, g)

    async def broken(grant: Any, refresh_token: str) -> dict[str, Any]:
        raise RuntimeError("ClickHouse away")

    async def cancel_mid_rollback() -> None:
        monkeypatch.setattr(oauth.store, "save_refresh", slow_save)
        monkeypatch.setattr(oauth, "_refresh_session", broken)
        form = {"grant_type": "refresh_token", "refresh_token": token, "client_id": "loom-cli"}
        task = asyncio.create_task(oauth._refresh_token_grant(form))
        await started.wait()
        task.cancel()
        go.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await shielded.drain()

    asyncio.run(cancel_mid_rollback())
    monkeypatch.setattr(oauth.store, "save_refresh", original_save)
    monkeypatch.setattr(oauth, "_refresh_session", original_session)
    assert _refresh(c, token).status_code == 200


def test_a_refresh_ends_well_before_a_spend_counts_as_abandoned() -> None:
    from loom.credentials import _REFRESH_TIMEOUT_S

    from server.auth import claims, oauth_store

    # The deadline, then the put-back, each ending in a cancelled write's undo.
    budget = oauth._REFRESH_DEADLINE_S + claims.UNDO_S + oauth._PUT_BACK_S + claims.UNDO_S
    assert budget < _REFRESH_TIMEOUT_S < oauth_store._ABANDONED_S


def test_refresh_needs_the_client_it_was_issued_to() -> None:
    c = _client()
    tokens = _signed_in_tokens(c)
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": "x",
        },
    )
    assert _error(r) == "invalid_grant"
    # Refused unconsumed: the right client still refreshes.
    assert _refresh(c, tokens["refresh_token"]).status_code == 200


# ── revocation (`loom logout`) ────────────────────────────────────────────


def test_revoking_a_refresh_token_ends_the_session(_world) -> None:
    c = _client()
    tokens = _signed_in_tokens(c)
    r = c.post(
        "/oauth/revoke",
        data={"token": tokens["refresh_token"], "client_id": device.DEVICE_CLIENT_ID},
    )
    assert r.status_code == 200
    jti = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})["sid"]
    assert _world["revoked"] == [jti]
    assert _error(_refresh(c, tokens["refresh_token"])) == "invalid_grant"


def test_revoking_a_session_access_token_lasts_as_long_as_its_refresh(_world) -> None:
    access = mint_token(
        subject="bob",
        org="acme",
        namespaces=["bob"],
        roles=["writer"],
        ttl_seconds=60,
        session_id="s1",
    )
    r = _client().post("/oauth/revoke", data={"token": access, "client_id": "loom-cli"})
    assert r.status_code == 200 and _world["revoked"] == ["s1"]
    # The session's refresh token lives 30 days, not the access token's minute.
    assert _world["expiries"][0] - datetime.now(UTC) > timedelta(days=29)


def test_revoking_another_token_revokes_its_jti_until_it_expires(_world) -> None:
    access = mint_token(
        subject="bob", org="acme", namespaces=["bob"], roles=["writer"], ttl_seconds=60, jti="j1"
    )
    _client().post("/oauth/revoke", data={"token": access, "client_id": "loom-cli"})
    assert _world["revoked"] == ["j1"]
    assert timedelta(0) < _world["expiries"][0] - datetime.now(UTC) <= timedelta(seconds=61)


def test_an_end_user_token_revokes_nothing_here(_world, monkeypatch) -> None:
    """Its `jti` is its issuer's to pick, and the revocation list spans the
    deployment: revoking it here could name another org's token or subject."""
    from server.settings import settings
    from tests import end_user_issuer

    private, public = end_user_issuer.new_key()
    monkeypatch.setattr(
        settings, "jwt_end_user_issuers_json", end_user_issuer.issuers_setting(public)
    )
    token = end_user_issuer.mint("acme", "u-1", private_pem=private, jti="bob@globex.io")
    from server.auth.jwt import verify_token

    assert verify_token(token).subject == "user:u-1", "a token this deployment accepts"
    r = _client().post("/oauth/revoke", data={"token": token, "client_id": "loom-cli"})
    assert r.status_code == 200 and _world["revoked"] == []


def test_revoking_garbage_is_a_quiet_200(_world) -> None:
    r = _client().post("/oauth/revoke", data={"token": "nonsense", "client_id": "loom-cli"})
    assert r.status_code == 200 and _world["revoked"] == []


# ── OIDC: the approval page signs in at the identity provider ─────────────


@pytest.fixture
def sso(monkeypatch):
    """Provider oidc with a stub IdP: sign-in redirects, the callback signs in
    `email`, and a refresh at the IdP does what `upstream` says."""
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    # The TestClient's own origin, so the state cookie (Secure only on https) comes back.
    monkeypatch.setattr(oauth.settings, "oauth_issuer", "http://testserver")
    idp: dict[str, Any] = {"email": "alice@example.com", "upstream": "ok", "refresh": "idp-r1"}

    async def build(
        *, redirect_uri: str, state: str, nonce: str, code_challenge: str, offline: bool
    ) -> str:
        return f"https://idp.example/authorize?state={state}"

    async def exchange(**kw: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"id_token": "t"}
        if idp["refresh"]:
            out["refresh_token"] = idp["refresh"]
        return out

    async def verify(token: str, *, nonce: str) -> dict[str, Any]:
        return {"email": idp["email"], "email_verified": True}

    async def issuer_ok(iss: str) -> bool:
        return iss in ("", "https://idp.example")

    async def refresh(token: str) -> str:
        if idp["upstream"] == "refused":
            raise oidc.UpstreamRefused
        if idp["upstream"] == "down":
            raise oidc.UpstreamUnavailable("ConnectError")
        return token + "+"

    async def by_email(email: str) -> Any:
        return _USER if email == "alice@example.com" else None

    async def no_jit(email: str) -> None:
        return None

    monkeypatch.setattr(oidc, "build_authorize_url", build)
    monkeypatch.setattr(oidc, "exchange_code", exchange)
    monkeypatch.setattr(oidc, "verify_id_token", verify)
    monkeypatch.setattr(oidc, "check_callback_issuer", issuer_ok)
    monkeypatch.setattr(oidc, "refresh", refresh)
    monkeypatch.setattr(login, "resolve_identity_for_email", by_email)
    monkeypatch.setattr(jit, "jit_provision_for_email", no_jit)
    return idp


def _sso_round_trip(c: TestClient, user_code: str, iss: str = "") -> httpx.Response:
    r = c.post("/oauth/device", data={"user_code": user_code}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://idp.example/")
    state = r.headers["location"].split("state=")[1]
    params = {"code": "idp-code", "state": state}
    if iss:
        params["iss"] = iss
    return c.get("/oauth/callback", params=params, follow_redirects=False)


def test_sso_device_sign_in_is_approved_on_the_same_confirm_page(sso) -> None:
    c = _client()
    grant = _start(c)
    page = _sso_round_trip(c, grant["user_code"], iss="https://idp.example")
    assert page.status_code == 200 and "alice@example.com" in page.text
    _approve(c, grant["user_code"], page.text)
    assert "refresh_token" in _poll(c, grant["device_code"]).json()


def test_a_code_approved_while_its_owner_was_at_the_idp_says_so(sso) -> None:
    owner, other = _client(), _client()
    grant = _start(owner)
    r = owner.post("/oauth/device", data={"user_code": grant["user_code"]}, follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    _approve(other, grant["user_code"], _sso_round_trip(other, grant["user_code"]).text)
    page = owner.get("/oauth/callback", params={"code": "c", "state": state})
    assert page.status_code == 400 and "already used" in page.text


def test_a_refusal_after_the_idp_rotated_revokes_the_new_idp_token(sso, monkeypatch) -> None:
    from server.auth import revocation

    revoked: list[str] = []

    async def idp_revoke(token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(oidc, "revoke", idp_revoke)
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sso_round_trip(c, grant["user_code"]).text)
    tokens = _poll(c, grant["device_code"]).json()
    revocation._apply_locally("alice@example.com", None)  # revoked between the IdP and the mint
    assert _refresh(c, tokens["refresh_token"]).status_code == 400
    assert [t for t in revoked if t] == ["idp-r1+"]  # what the IdP handed back


@pytest.fixture
def bobs_password_account(sso, monkeypatch):
    """Bob signs in at the IdP; Loom has his account from before SSO, made with
    a password and not linked yet."""
    sso["email"] = "bob@example.com"
    bob = ("bob@example.com", "acme", ["bob"], ["writer"])
    links: list[tuple[str, list[tuple[str, str]], str]] = []

    async def by_email(email: str) -> Any:
        return bob if links else None

    stored = hash_password("bobpw")

    async def unlinked(email: str) -> list[dict[str, Any]]:
        return [] if links else [{"org": "acme", "namespace": "bob", "password_hash": stored}]

    async def link(email: str, accounts: list[tuple[str, str]], by: str) -> list[tuple[str, str]]:
        links.append((email, accounts, by))
        return accounts

    async def credentials(identifier: str, password: str, *, address: str = "") -> Any:
        return bob if (identifier, password) == ("bob@example.com", "bobpw") else None

    async def no_jit(email: str) -> None:
        raise AssertionError("JIT must not make a second account")

    monkeypatch.setattr(login, "resolve_identity_for_email", by_email)
    monkeypatch.setattr(login, "resolve_user_credentials", credentials)
    monkeypatch.setattr(sso_links, "unlinked_accounts", unlinked)
    monkeypatch.setattr(sso_links, "link_live", link)
    monkeypatch.setattr(jit, "jit_provision_for_email", no_jit)
    return links


def _link(c: TestClient, page: str, password: str) -> httpx.Response:
    return c.post("/oauth/link", data={"ticket": _ticket(page), "password": password})


def test_sso_reaches_a_password_account_once_its_password_is_proven(
    bobs_password_account, _world
) -> None:
    c = _client()
    grant = _start(c)
    page = _sso_round_trip(c, grant["user_code"])
    assert page.status_code == 200 and "Link your Loom account" in page.text
    wrong = _link(c, page.text, "guess")
    assert wrong.status_code == 401 and not bobs_password_account
    confirm = _link(c, wrong.text, "bobpw")
    assert confirm.status_code == 200 and "bob@example.com" in confirm.text
    assert bobs_password_account == [("bob@example.com", [("acme", "bob")], "self")]
    _approve(c, grant["user_code"], confirm.text)
    tokens = _poll(c, grant["device_code"]).json()
    claims = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})
    assert claims["sub"] == "bob@example.com" and claims["namespaces"] == ["bob"]
    assert "auth.sso_linked" in [n for n, _ in _world["events"]]


def test_declining_someone_elses_password_account_makes_no_second_one(
    bobs_password_account, _world, monkeypatch
) -> None:
    # Whoever registered bob's address with a password is not bob, but an
    # account of bob's own beside it in the same org would name the same
    # email, and every grant made to that email would reach both: he is sent
    # to the operator instead (the fixture's JIT raises if it runs).
    from server.settings import settings

    monkeypatch.setattr(settings, "oidc_jit_org", "acme")
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])
    r = c.post("/oauth/link", data={"ticket": _ticket(page.text), "decision": "not_mine"})
    assert r.status_code == 403 and "release it" in r.text
    assert not bobs_password_account  # nothing linked
    assert "auth.sso_link_declined" in [n for n, _ in _world["events"]]


def test_declining_a_password_account_in_another_org_goes_on_to_ones_own(
    bobs_password_account, monkeypatch
) -> None:
    # The squatter's account is in another org than JIT's: grants there tie
    # nothing to an account in the JIT org, so bob gets his own.
    from server.settings import settings

    own = ("u-bob-1a2b3c4d", "corp", ["u-bob-1a2b3c4d"], ["writer"])

    async def jit_account(email: str) -> Any:
        return own

    monkeypatch.setattr(settings, "oidc_jit_org", "corp")
    monkeypatch.setattr(jit, "jit_provision_for_email", jit_account)
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])
    r = c.post("/oauth/link", data={"ticket": _ticket(page.text), "decision": "not_mine"})
    assert r.status_code == 200 and "u-bob-1a2b3c4d" in r.text
    assert not bobs_password_account  # nothing linked


def test_declining_another_orgs_account_with_jit_off_is_denied_and_ends_the_grant(
    bobs_password_account, _world, monkeypatch
) -> None:
    from server.settings import settings

    async def no_jit(email: str) -> None:
        return None  # JIT off: no account is made

    monkeypatch.setattr(settings, "oidc_jit_org", "corp")
    monkeypatch.setattr(jit, "jit_provision_for_email", no_jit)
    c = _client()
    grant = _start(c)
    page = _sso_round_trip(c, grant["user_code"])
    r = c.post("/oauth/link", data={"ticket": _ticket(page.text), "decision": "not_mine"})
    assert r.status_code == 403 and "no loom account for this email" in r.text
    assert _error(_poll(c, grant["device_code"])) == "access_denied"
    denied = [s for n, s in _world["events"] if n == "auth.signin_denied"]
    assert denied and "ask your" not in str(denied[-1])  # the bare reason, not the page's advice


def test_declining_while_jit_cannot_read_starts_again(bobs_password_account, monkeypatch) -> None:
    from server.auth.login import SignInUnavailable
    from server.settings import settings

    revoked: list[str] = []

    async def unavailable(email: str) -> None:
        raise SignInUnavailable

    async def revoke(token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(settings, "oidc_jit_org", "corp")
    monkeypatch.setattr(jit, "jit_provision_for_email", unavailable)
    monkeypatch.setattr(oidc, "revoke", revoke)
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])
    r = c.post("/oauth/link", data={"ticket": _ticket(page.text), "decision": "not_mine"})
    assert r.status_code == 503 and "/oauth/device?user_code=" in r.text
    assert revoked == ["idp-r1"]


def test_linked_accounts_that_disagree_make_no_new_one(sso, monkeypatch) -> None:
    # The email's linked accounts resolve to nobody (two owners): JIT must not
    # add a third beside them.
    sso["email"] = "carol@example.com"

    async def nobody(email: str) -> None:
        return None

    async def linked(email: str) -> list[tuple[str, str]]:
        return [("acme", "aa-sq")]

    async def no_jit(email: str) -> None:
        raise AssertionError("JIT must not run")

    monkeypatch.setattr(login, "resolve_identity_for_email", nobody)
    monkeypatch.setattr(sso_links, "reaching", linked)
    monkeypatch.setattr(jit, "jit_provision_for_email", no_jit)
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])
    assert page.status_code == 403 and "linked accounts" in page.text


def test_a_link_ticket_finishes_nothing_in_another_browser(bobs_password_account) -> None:
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])
    elsewhere = _client()  # the ticket, without the browser that got it
    assert _link(elsewhere, page.text, "bobpw").status_code == 400
    assert not bobs_password_account
    assert _link(c, page.text, "bobpw").status_code == 200


def test_a_link_that_fails_once_spent_starts_again(bobs_password_account, monkeypatch) -> None:
    revoked: list[str] = []

    async def down(email: str, accounts: list[tuple[str, str]], by: str) -> None:
        raise RuntimeError("ClickHouse away")

    async def revoke(token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(sso_links, "link_live", down)
    monkeypatch.setattr(oidc, "revoke", revoke)
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])
    r = _link(c, page.text, "bobpw")
    assert r.status_code == 503 and "/oauth/device?user_code=" in r.text
    assert revoked == ["idp-r1"]


def test_two_link_pages_in_one_browser_keep_their_own_binding(bobs_password_account) -> None:
    c = _client()
    first = _sso_round_trip(c, _start(c)["user_code"])
    _sso_round_trip(c, _start(c)["user_code"])  # a second sign-in, in another tab
    assert _link(c, first.text, "bobpw").status_code == 200


def test_a_link_page_open_across_a_deactivation_writes_nothing(
    bobs_password_account, monkeypatch
) -> None:
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])

    async def deactivated(email: str) -> bool:
        return email == "bob@example.com"  # SCIM deactivated bob while the page was open

    monkeypatch.setattr(scim, "deactivated", deactivated)
    r = _link(c, page.text, "bobpw")
    assert r.status_code == 403 and "deactivated" in r.text
    assert not bobs_password_account  # no link: it would be reachable but never revoked


def test_a_link_ticket_is_spent_once(bobs_password_account) -> None:
    c = _client()
    page = _sso_round_trip(c, _start(c)["user_code"])
    assert _link(c, page.text, "bobpw").status_code == 200
    assert _link(c, page.text, "bobpw").status_code == 400


def test_a_made_up_link_ticket_is_refused(bobs_password_account) -> None:
    r = _client().post("/oauth/link", data={"ticket": "forged", "password": "bobpw"})
    assert r.status_code == 400 and not bobs_password_account


def test_sso_page_offers_no_password_form(sso) -> None:
    page = _client().get("/oauth/device")
    assert "single sign-on" in page.text and 'name="password"' not in page.text


def test_a_callback_naming_another_issuer_is_refused(sso) -> None:
    c = _client()
    r = _sso_round_trip(c, _start(c)["user_code"], iss="https://evil.example")
    assert (r.status_code, r.json()["error"]) == (400, "invalid_issuer")


def test_sso_without_an_account_denies_the_device(sso, _world) -> None:
    sso["email"] = "mallory@elsewhere.example"
    c = _client()
    grant = _start(c)
    page = _sso_round_trip(c, grant["user_code"])
    assert page.status_code == 403 and "no loom account" in page.text
    assert _error(_poll(c, grant["device_code"])) == "access_denied"
    assert [n for n, _ in _world["events"]] == ["auth.signin_denied"]


def test_sso_without_an_idp_refresh_token_issues_no_refresh_token(sso) -> None:
    sso["refresh"] = ""
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sso_round_trip(c, grant["user_code"]).text)
    tokens = _poll(c, grant["device_code"]).json()
    assert tokens["access_token"] and "refresh_token" not in tokens


def _sso_tokens(c: TestClient) -> dict[str, Any]:
    grant = _start(c)
    _approve(c, grant["user_code"], _sso_round_trip(c, grant["user_code"]).text)
    tokens: dict[str, Any] = _poll(c, grant["device_code"]).json()
    return tokens


def test_refresh_asks_the_identity_provider_and_keeps_its_rotation(sso, _world) -> None:
    c = _client()
    tokens = _sso_tokens(c)
    assert _refresh(c, tokens["refresh_token"]).status_code == 200
    kept = [g["upstream_refresh"] for g in _world["store"].refresh.values()]
    assert "idp-r1+" in kept


def test_a_user_the_identity_provider_refuses_is_cut_off_at_refresh(sso, _world) -> None:
    c = _client()
    tokens = _sso_tokens(c)
    sso["upstream"] = "refused"
    assert _error(_refresh(c, tokens["refresh_token"])) == "invalid_grant"
    # The session only: the IdP says this for an expired refresh token too.
    sid = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})["sid"]
    assert _world["revoked"] == [sid]
    reasons = [kw["subject"]["reason"] for n, kw in _world["events"] if n == "auth.session_revoked"]
    assert reasons == ["identity provider refused the user"]


def test_an_unreachable_identity_provider_leaves_the_refresh_token_usable(sso) -> None:
    c = _client()
    tokens = _sso_tokens(c)
    sso["upstream"] = "down"
    assert _refresh(c, tokens["refresh_token"]).status_code == 503
    sso["upstream"] = "ok"
    assert _refresh(c, tokens["refresh_token"]).status_code == 200


def test_a_password_session_cannot_refresh_once_sso_is_on(monkeypatch) -> None:
    c = _client()
    tokens = _signed_in_tokens(c)
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    assert _error(_refresh(c, tokens["refresh_token"])) == "invalid_grant"


def test_an_sso_session_cannot_refresh_once_sso_is_off(sso, monkeypatch) -> None:
    # The IdP no longer vouches at each refresh: the session ends, and its
    # owner signs in the way Loom now signs people in.
    c = _client()
    tokens = _sso_tokens(c)
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "password")
    r = _refresh(c, tokens["refresh_token"])
    assert _error(r) == "invalid_grant" and "sign in again" in r.json()["error_description"]


# ── dashboard SSO and the closed password door ────────────────────────────


def test_dashboard_sso_is_404_without_oidc() -> None:
    assert _client().get("/oauth/sso", follow_redirects=False).status_code == 404


def test_dashboard_sso_hands_back_a_short_token_in_the_fragment(sso, _world) -> None:
    c = _client()
    r = c.get("/oauth/sso", follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    back = c.get("/oauth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert back.status_code == 302 and back.headers["location"].startswith("/me#token=")
    # The sign-in landing page: the dashboard served without the aggregator
    # redirect, which would carry the fragment to another origin
    # (tests/unit/test_dashboard_routes.py).
    target = back.headers["location"].split("#", 1)[0]
    assert target == "/me"
    token = back.headers["location"].split("#token=")[1]
    claims = pyjwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 3600 and claims["sid"]
    assert [n for n, _ in _world["events"]] == ["auth.sso_signin"]


def test_a_revoked_person_gets_no_dashboard_token(sso, _world, monkeypatch) -> None:
    """Like every mint, the dashboard's reads revocations: a person revoked
    since gets a refusal, never a token."""
    from server.auth import revocation

    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({_USER[0]}))
    c = _client()
    state = c.get("/oauth/sso", follow_redirects=False).headers["location"].split("state=")[1]
    back = c.get("/oauth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert back.status_code == 403 and "revoked" in back.text and "#token=" not in back.text
    assert [n for n, _ in _world["events"]] == ["auth.signin_denied"]


def test_password_login_is_closed_under_sso(monkeypatch) -> None:
    from fastapi import FastAPI

    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    app = FastAPI()
    app.include_router(login.router)
    r = TestClient(app).post("/v1/auth/login", json={"identifier": "a", "password": "b"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "PASSWORD_LOGIN_SSO"


# ── IdP round trips: one browser, never left hanging ────────────────────────


def test_a_callback_from_another_browser_is_refused(sso) -> None:
    c = _client()
    grant = _start(c)
    r = c.post("/oauth/device", data={"user_code": grant["user_code"]}, follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    other_browser = _client()  # no state cookie
    back = other_browser.get("/oauth/callback", params={"code": "c", "state": state})
    assert back.status_code == 400 and "expired" in back.text
    # The browser that started it still can.
    assert c.get("/oauth/callback", params={"code": "c", "state": state}).status_code == 200


def test_the_pending_sign_in_holds_no_usable_user_code(sso, _world) -> None:
    c = _client()
    grant = _start(c)
    c.post("/oauth/device", data={"user_code": grant["user_code"]}, follow_redirects=False)
    pending = str(_world["store"].records("pending"))
    assert device.normalize_user_code(grant["user_code"]) not in pending


def test_cancelling_at_the_identity_provider_leaves_the_grant_to_retry(sso, _world) -> None:
    c = _client()
    grant = _start(c)
    r = c.post("/oauth/device", data={"user_code": grant["user_code"]}, follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    back = c.get("/oauth/callback", params={"error": "access_denied", "state": state})
    assert back.status_code == 403 and "try again" in back.text
    # Whoever cancelled is unknown, so nobody's sign-in ends: the user retries.
    assert _error(_poll(c, grant["device_code"])) == "authorization_pending"
    page = _sso_round_trip(c, grant["user_code"])
    _approve(c, grant["user_code"], page.text)
    assert "access_token" in _poll(c, grant["device_code"]).json()


def test_a_strangers_failed_sign_in_does_not_end_someone_elses_grant(sso) -> None:
    c = _client()
    grant = _start(c)
    victim_page = _sso_round_trip(c, grant["user_code"])  # alice signs in first
    sso["email"] = "stranger@elsewhere.example"
    assert _sso_round_trip(c, grant["user_code"]).status_code == 403
    _approve(c, grant["user_code"], victim_page.text)
    assert "access_token" in _poll(c, grant["device_code"]).json()


def test_dashboard_sso_starts_are_throttled_per_address(sso, monkeypatch) -> None:
    monkeypatch.setattr(oauth, "_starts_by_address", oauth.Attempts("oidc.start", 30, 900))
    c = _client()
    for _ in range(30):
        assert c.get("/oauth/sso", follow_redirects=False).status_code == 302
    assert c.get("/oauth/sso", follow_redirects=False).status_code == 429


def _register(c: TestClient) -> str:
    r = c.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1:9/cb"]})
    return str(r.json()["client_id"])


def test_no_password_reaches_mcp_connect_under_sso(sso) -> None:
    c = _client()
    r = c.post(
        "/oauth/authorize",
        data={
            "client_id": _register(c),
            "redirect_uri": "http://127.0.0.1:9/cb",
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
            "identifier": "alice@example.com",
            "password": "pw",
        },
        follow_redirects=False,
    )
    assert r.status_code == 404


def _connect_through_sso(c: TestClient) -> httpx.Response:
    r = c.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": _register(c),
            "redirect_uri": "http://127.0.0.1:9/cb",
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
            "state": "mcp-state",
        },
        follow_redirects=False,
    )
    state = r.headers["location"].split("state=")[1]
    return c.get("/oauth/callback", params={"code": "c", "state": state}, follow_redirects=False)


def test_mcp_connect_under_sso_asks_before_it_sends_a_code(sso, _world) -> None:
    c = _client()
    page = _connect_through_sso(c)
    assert (
        page.status_code == 200
        and "127.0.0.1:9" in page.text
        and not _world["store"].records("code")
    )
    ticket = _ticket(page.text)
    r = c.post(
        "/oauth/authorize/approve",
        data={"ticket": ticket, "decision": "approve"},
        follow_redirects=False,
    )
    assert r.status_code == 302 and "code=" in r.headers["location"]
    assert "state=mcp-state" in r.headers["location"]
    again = c.post("/oauth/authorize/approve", data={"ticket": ticket, "decision": "approve"})
    assert again.status_code == 400
    assert "auth.connect_approved" in [n for n, _ in _world["events"]]


def test_a_connect_approved_after_a_minute_still_gives_a_live_code(
    sso, _world, monkeypatch
) -> None:
    c = _client()
    ticket = _ticket(_connect_through_sso(c).text)
    later = time.time() + 65  # the person read the page for a while
    monkeypatch.setattr(time, "time", lambda: later)
    r = c.post(
        "/oauth/authorize/approve",
        data={"ticket": ticket, "decision": "approve"},
        follow_redirects=False,
    )
    (code,) = _world["store"].records("code").values()
    assert r.status_code == 302 and code["exp"] > later


def test_denying_mcp_connect_sends_the_client_an_error(sso, _world) -> None:
    c = _client()
    ticket = _ticket(_connect_through_sso(c).text)
    r = c.post(
        "/oauth/authorize/approve",
        data={"ticket": ticket, "decision": "deny"},
        follow_redirects=False,
    )
    assert "error=access_denied" in r.headers["location"] and not _world["store"].records("code")


async def test_logout_survives_an_unreachable_identity_provider(monkeypatch) -> None:
    async def down() -> dict[str, Any]:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(oidc, "_discover", down)
    await oidc.revoke("idp-refresh")  # logged, not raised


# ── revocation that lasts, parallel sign-ins, IdP tokens ────────────────────


def test_revoking_a_long_lived_token_lasts_as_long_as_the_token(_world) -> None:
    year = mint_token(
        subject="bot", org="acme", namespaces=["x"], roles=["writer"], ttl_seconds=365 * 86400
    )
    forever = mint_token(subject="bot", org="acme", namespaces=["x"], roles=["writer"])
    c = _client()
    c.post("/oauth/revoke", data={"token": year, "client_id": "loom-cli"})
    c.post("/oauth/revoke", data={"token": forever, "client_id": "loom-cli"})
    assert _world["expiries"][0] - datetime.now(UTC) > timedelta(days=364)
    assert _world["expiries"][1] is None  # the revocation module keeps it for as long as it allows


def test_revocation_needs_a_client_id(_world) -> None:
    tokens = _signed_in_tokens(_client())
    r = _client().post("/oauth/revoke", data={"token": tokens["refresh_token"]})
    assert (r.status_code, _error(r)) == (400, "invalid_request")


def test_two_sign_ins_at_once_in_one_browser_both_finish(sso) -> None:
    c = _client()
    first, second = _start(c), _start(c)
    states = []
    for grant in (first, second):
        r = c.post("/oauth/device", data={"user_code": grant["user_code"]}, follow_redirects=False)
        states.append(r.headers["location"].split("state=")[1])
    for state in states:
        assert c.get("/oauth/callback", params={"code": "c", "state": state}).status_code == 200


def test_only_refreshing_sign_ins_ask_for_an_idp_refresh_token(sso, monkeypatch) -> None:
    asked: dict[str, bool] = {}

    async def build(
        *, redirect_uri: str, state: str, nonce: str, code_challenge: str, offline: bool
    ) -> str:
        asked[state] = offline
        return f"https://idp.example/authorize?state={state}"

    monkeypatch.setattr(oidc, "build_authorize_url", build)
    c = _client()
    c.get("/oauth/sso", follow_redirects=False)
    c.post("/oauth/device", data={"user_code": _start(c)["user_code"]}, follow_redirects=False)
    assert list(asked.values()) == [False, True]


def test_denying_after_sso_revokes_the_idp_token(sso, monkeypatch) -> None:
    revoked: list[str] = []

    async def revoke(token: str) -> None:
        if token:  # as the real one, which does nothing for ''
            revoked.append(token)

    monkeypatch.setattr(oidc, "revoke", revoke)
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sso_round_trip(c, grant["user_code"]).text, decision="deny")
    assert revoked == ["idp-r1"]


def test_user_codes_are_keyed_with_the_server_secret(_world) -> None:
    import hashlib

    grant = _start(_client())
    stored = next(iter(_world["store"].records("device").values()))["user_code"]
    plain = hashlib.sha256(device.normalize_user_code(grant["user_code"]).encode()).hexdigest()
    assert stored != plain and len(stored) == 64


# ── sessions and the tokens they mint ───────────────────────────────────────


def test_ending_a_session_refuses_its_access_tokens() -> None:
    from server.auth.jwt import TokenError, verify_token

    token = mint_token(
        subject="ann",
        org="acme",
        namespaces=["ann"],
        roles=["writer"],
        ttl_seconds=3600,
        session_id="s1",
    )
    assert verify_token(token).session_id == "s1"
    revocation._apply_locally("s1", None)
    with pytest.raises(TokenError, match="session"):
        verify_token(token)


def test_a_child_of_a_session_lives_as_long_as_asked() -> None:
    from server.auth import delegation
    from server.auth.jwt import verify_token

    def parent(**kw: Any) -> Any:
        return verify_token(
            mint_token(
                subject="ann",
                org="acme",
                namespaces=["ann"],
                roles=["writer"],
                ttl_seconds=3600,
                **kw,
            )
        )

    from fastapi import HTTPException

    ninety_days, never = 90 * 86400, delegation.NEVER
    session = parent(session_id="s1", first_party=True)
    # Not the parent's hour: what the person asked for, one that never expires
    # included, for a token they can see and revoke in their list.
    assert delegation._child_ttl(session, ninety_days) == ninety_days
    assert delegation._child_ttl(session, None) == ninety_days  # the default
    assert delegation._child_ttl(session, never) is None
    # Any other parent bounds it, and cannot make one that never expires: a
    # third-party MCP client's session among them.
    for other in (parent(), parent(session_id="s2")):
        assert delegation._child_ttl(other, ninety_days) <= 3600
        with pytest.raises(HTTPException, match="cannot make one that never does"):
            delegation._child_ttl(other, never)


def test_only_loom_logins_own_sessions_are_first_party(_world) -> None:
    from server.auth import oauth
    from server.auth.device import DEVICE_CLIENT_ID
    from server.auth.jwt import verify_token

    grant: Any = {
        "subject": "ann",
        "org": "acme",
        "namespaces": ["ann"],
        "roles": ["writer"],
        "upstream_refresh": "",
        "held": {},
    }

    def first_party(client_id: str) -> bool:
        session = {**grant, "family": f"fam-{client_id}"}
        body = asyncio.run(oauth.issue_tokens(session, client_id, since=0))
        return verify_token(body["access_token"]).first_party

    assert first_party(DEVICE_CLIENT_ID) is True
    assert first_party("some-mcp-client") is False


def test_a_refreshed_loom_login_and_the_dashboards_sso_stay_first_party(_world) -> None:
    from server.auth.jwt import verify_token

    c = _client()
    tokens = _signed_in_tokens(c)
    assert verify_token(tokens["access_token"]).first_party
    rotated = _refresh(c, tokens["refresh_token"]).json()
    assert verify_token(rotated["access_token"]).first_party
    ann = ("ann", "acme", ["ann"], ["writer"])
    signed_in = asyncio.run(oauth._dashboard_signed_in(ann, {"kind": "dashboard"}))
    token = signed_in.headers["location"].split("#token=", 1)[1]
    assert verify_token(token).first_party and verify_token(token).session_id


def test_two_people_on_one_code_end_the_grant_for_both(_world, monkeypatch) -> None:
    mallory = ("mallory@example.com", "acme", ["mallory"], ["writer"])

    async def credentials(identifier: str, password: str, *, address: str = "") -> Any:
        return {"alice@example.com": _USER, "mallory@example.com": mallory}.get(identifier)

    monkeypatch.setattr(login, "resolve_user_credentials", credentials)
    c = _client()
    # Whoever signs in first, the terminal is signed in as neither.
    for first, second in (
        ("alice@example.com", "mallory@example.com"),
        ("mallory@example.com", "alice@example.com"),
    ):
        grant = _start(c)
        page = c.post(
            "/oauth/device",
            data={"user_code": grant["user_code"], "identifier": first, "password": "x"},
        )
        seen = c.post(
            "/oauth/device",
            data={"user_code": grant["user_code"], "identifier": second, "password": "x"},
        )
        assert seen.status_code == 409 and "Someone else already used this code" in seen.text
        assert _approve(c, grant["user_code"], page.text).status_code == 400
        assert _error(_poll(c, grant["device_code"])) == "access_denied"


def test_the_same_name_in_another_org_is_another_person(_world, monkeypatch) -> None:
    other_alice = ("alice@example.com", "globex", ["alice"], ["writer"])

    async def credentials(identifier: str, password: str, *, address: str = "") -> Any:
        return {"acme": _USER, "globex": other_alice}.get(password)

    monkeypatch.setattr(login, "resolve_user_credentials", credentials)
    c = _client()
    grant = _start(c)
    for org in ("acme", "globex"):
        seen = c.post(
            "/oauth/device",
            data={
                "user_code": grant["user_code"],
                "identifier": "alice@example.com",
                "password": org,
            },
        )
    assert seen.status_code == 409
    assert _error(_poll(c, grant["device_code"])) == "access_denied"


def test_a_refused_sso_sign_in_revokes_the_idp_token_it_got(sso, monkeypatch) -> None:
    revoked: list[str] = []

    async def revoke(token: str) -> None:
        if token:  # as the real one, which does nothing for ''
            revoked.append(token)

    monkeypatch.setattr(oidc, "revoke", revoke)
    sso["email"] = "stranger@elsewhere.example"
    c = _client()
    _sso_round_trip(c, _start(c)["user_code"])
    assert revoked == ["idp-r1"]


# ── a replay ends the session, never the person ─────────────────────────


def test_a_revoked_subject_cannot_sign_in_again(_world) -> None:
    revocation._apply_locally("alice@example.com", None)
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sign_in(c, grant["user_code"]).text)
    assert _error(_poll(c, grant["device_code"])) == "invalid_grant"
    # And replaying that grant writes nothing that could soften the ban.
    _poll(c, grant["device_code"])
    assert "alice@example.com" not in _world["revoked"]


def test_replaying_a_denied_grant_revokes_nothing(_world) -> None:
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sign_in(c, grant["user_code"]).text, decision="deny")
    assert _error(_poll(c, grant["device_code"])) == "access_denied"
    assert _error(_poll(c, grant["device_code"])) == "access_denied"  # not told a session ended
    assert _world["revoked"] == []


def test_a_failed_issue_leaves_the_code_unspent(_world, monkeypatch) -> None:
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sign_in(c, grant["user_code"]).text)
    real = _world["store"].save_refresh

    async def flaky(token: str, g: dict[str, Any]) -> None:
        monkeypatch.setattr(_world["store"], "save_refresh", real)
        raise RuntimeError("insert failed")

    monkeypatch.setattr(_world["store"], "save_refresh", flaky)
    assert _poll(c, grant["device_code"]).status_code == 503  # told to retry
    assert "access_token" in _poll(c, grant["device_code"]).json()  # the retry is no replay
    assert _world["revoked"] == []


# ── lost answers and leaked codes ────────────────────


def test_a_lost_answer_costs_a_sign_in_and_nothing_else(_world) -> None:
    """Rotation cannot tell a lost answer from a copy: the session ends, as
    for a replay, and the owner signs in again. What it minted is untouched."""
    c = _client()
    first = _signed_in_tokens(c)
    _refresh(c, first["refresh_token"])  # its answer never reached the client
    assert _error(_refresh(c, first["refresh_token"])) == "invalid_grant"
    sid = pyjwt.decode(first["access_token"], options={"verify_signature": False})["sid"]
    assert _world["revoked"] == [sid]


def test_a_loom_failure_during_refresh_leaves_the_token_usable(_world, monkeypatch) -> None:
    c = _client()
    tokens = _signed_in_tokens(c)

    async def ch_down(subject: str, org: str, roles: list[str]) -> list[str]:
        raise RuntimeError("ClickHouse unreachable")

    monkeypatch.setattr(login, "refreshed_roles", ch_down)
    assert _refresh(c, tokens["refresh_token"]).status_code == 503  # told to retry

    async def active(subject: str, org: str, roles: list[str]) -> list[str]:
        return roles

    monkeypatch.setattr(login, "refreshed_roles", active)
    assert _refresh(c, tokens["refresh_token"]).status_code == 200  # no replay
    assert _world["revoked"] == []


def _code_for(c: TestClient, store: MemoryOAuthStore) -> tuple[str, str, str]:
    """(client_id, code, verifier) for one MCP connect by password."""
    import base64
    import hashlib

    client_id = _register(c)
    verifier = "v" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    r = c.post(
        "/oauth/authorize",
        data={
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:9/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "identifier": "alice@example.com",
            "password": "pw",
        },
        follow_redirects=False,
    )
    code = r.headers["location"].split("code=")[1].split("&")[0]
    return client_id, code, verifier


def _exchange(c: TestClient, client_id: str, code: str, verifier: str) -> httpx.Response:
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:9/cb",
            "code_verifier": verifier,
        },
    )


def test_a_leaked_spent_code_alone_ends_nothing(_world, monkeypatch) -> None:
    c = _client()
    client_id, code, verifier = _code_for(c, _world["store"])
    assert _exchange(c, client_id, code, verifier).status_code == 200
    assert _error(_exchange(c, "someone-else", code, verifier)) == "invalid_grant"
    assert _error(_exchange(c, client_id, code, "w" * 43)) == "invalid_grant"
    assert _world["revoked"] == []
    # With the verifier, it is the client's own copy coming back: the session ends.
    assert _error(_exchange(c, client_id, code, verifier)) == "invalid_grant"
    assert len(_world["revoked"]) == 1


# ── failures put credentials back ───────────────────────────────────────────


def test_a_failed_code_exchange_leaves_the_code_unspent(_world, monkeypatch) -> None:
    c = _client()
    client_id, code, verifier = _code_for(c, _world["store"])
    real = _world["store"].save_refresh

    async def flaky(token: str, g: dict[str, Any]) -> None:
        monkeypatch.setattr(_world["store"], "save_refresh", real)
        raise RuntimeError("insert failed")

    monkeypatch.setattr(_world["store"], "save_refresh", flaky)
    assert _exchange(c, client_id, code, verifier).status_code == 503  # told to retry
    assert _exchange(c, client_id, code, verifier).status_code == 200
    assert _world["revoked"] == []


def test_an_inactive_account_ends_its_session_at_refresh(_world, monkeypatch) -> None:
    c = _client()
    tokens = _signed_in_tokens(c)

    async def gone(subject: str, org: str, roles: list[str]) -> None:
        return None

    monkeypatch.setattr(login, "refreshed_roles", gone)
    assert _error(_refresh(c, tokens["refresh_token"])) == "invalid_grant"
    sid = pyjwt.decode(tokens["access_token"], options={"verify_signature": False})["sid"]
    assert _world["revoked"] == [sid]


def test_a_replays_revocation_lapses_with_the_session(_world) -> None:
    c = _client()
    first = _signed_in_tokens(c)
    second = _refresh(c, first["refresh_token"]).json()
    _refresh(c, second["refresh_token"])
    _refresh(c, first["refresh_token"])
    lasts = [e - datetime.now(UTC) for e in _world["expiries"]]
    assert all(timedelta(days=29) < left <= timedelta(days=30) for left in lasts)


def test_discovery_always_names_its_response_types(monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_enabled", False)
    assert oauth.authorization_server_metadata("https://x")["response_types_supported"] == []


# ── wire format, owed revocations, the replay signal ──────────────────────


def test_oauth_errors_are_at_the_top_of_the_body() -> None:
    """RFC 8628 §3.5 / RFC 6749 §5.2: device clients read `error` there."""
    c = _client()
    r = _poll(c, _start(c)["device_code"])
    assert r.json() == {"error": "authorization_pending"}
    assert r.headers["cache-control"] == "no-store"


def test_a_replay_says_why_the_session_ended(_world) -> None:
    c = _client()
    first = _signed_in_tokens(c)
    _refresh(c, first["refresh_token"])
    r = _refresh(c, first["refresh_token"])
    assert r.json()["error"] == "invalid_grant"
    assert "presented again" in r.json()["error_description"]


def _failing_revoke(monkeypatch, *, owe_fails: bool, store: Any) -> None:
    async def down(token_id: str, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("revoked_tokens unwritable")

    monkeypatch.setattr(revocation, "revoke", down)
    if owe_fails:

        async def owe_down(family: str, reason: str) -> None:
            raise RuntimeError("oauth_state unwritable")

        monkeypatch.setattr(store, "owe_revocation", owe_down)


def test_a_revocation_that_cannot_be_written_is_owed_and_refuses_the_family(
    _world, monkeypatch
) -> None:
    c = _client()
    first = _signed_in_tokens(c)
    second = _refresh(c, first["refresh_token"]).json()
    _failing_revoke(monkeypatch, owe_fails=False, store=_world["store"])
    assert _error(_refresh(c, first["refresh_token"])) == "invalid_grant"  # the replay
    sid = pyjwt.decode(first["access_token"], options={"verify_signature": False})["sid"]
    assert sid in _world["store"].owed
    # The family's live token is refused while the debt stands.
    assert _error(_refresh(c, second["refresh_token"])) == "invalid_grant"


def test_a_replay_whose_end_is_recorded_nowhere_is_told_to_retry(_world, monkeypatch) -> None:
    c = _client()
    first = _signed_in_tokens(c)
    _refresh(c, first["refresh_token"])
    _failing_revoke(monkeypatch, owe_fails=True, store=_world["store"])
    r = _refresh(c, first["refresh_token"])
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"


def test_a_busy_grant_is_a_retry_not_a_refusal(_world, monkeypatch) -> None:
    from server.auth import claims

    c = _client()
    tokens = _signed_in_tokens(c)

    async def busy(token: str, client_id: str) -> None:
        raise claims.Busy("refresh:x")

    monkeypatch.setattr(_world["store"], "take_refresh", busy)
    r = _refresh(c, tokens["refresh_token"])
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"


def test_a_code_presented_wrongly_is_left_for_its_client(_world) -> None:
    c = _client()
    client_id, code, verifier = _code_for(c, _world["store"])
    assert _error(_exchange(c, client_id, code, "w" * 43)) == "invalid_grant"
    assert _exchange(c, client_id, code, verifier).status_code == 200  # not burned


def test_a_failed_access_token_logout_is_a_retry(_world, monkeypatch) -> None:
    async def down(token_id: str, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("revoked_tokens unwritable")

    monkeypatch.setattr(revocation, "revoke", down)
    access = mint_token(
        subject="bob", org="acme", namespaces=["bob"], roles=["writer"], ttl_seconds=60
    )
    r = _client().post("/oauth/revoke", data={"token": access, "client_id": "loom-cli"})
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"


def test_a_logout_retried_after_the_token_was_spent_is_still_a_logout(_world) -> None:
    c = _client()
    tokens = _signed_in_tokens(c)
    _refresh(c, tokens["refresh_token"])  # spent
    c.post("/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": "loom-cli"})
    reasons = [kw["subject"]["reason"] for n, kw in _world["events"] if n == "auth.session_revoked"]
    assert reasons == ["logout (token already spent)"]


def test_a_refresh_after_logout_is_refused_without_a_replay_verdict(_world) -> None:
    c = _client()
    tokens = _signed_in_tokens(c)
    c.post("/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": "loom-cli"})
    r = _refresh(c, tokens["refresh_token"])  # a stale process still holds it
    assert _error(r) == "invalid_grant"
    assert r.json()["error_description"] == "this session has ended; sign in again"
    reasons = [kw["subject"]["reason"] for n, kw in _world["events"] if n == "auth.session_revoked"]
    assert reasons == ["logout"]


def test_a_keeper_outage_at_the_first_step_is_a_retryable_503(_world, monkeypatch) -> None:
    from server.auth import claims

    async def down(d: dict[str, Any]) -> None:
        raise claims.Busy("Keeper away")

    monkeypatch.setattr(_world["store"], "save_device", down)
    r = _client().post("/oauth/device_authorization", data={"client_id": device.DEVICE_CLIENT_ID})
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"


def test_an_approval_whose_code_could_not_be_saved_can_be_submitted_again(
    sso, _world, monkeypatch
) -> None:
    from server.auth import claims

    c = _client()
    ticket = _ticket(_connect_through_sso(c).text)
    store = _world["store"]
    real = store.save_code

    async def flaky(code: str, g: dict[str, Any]) -> None:
        monkeypatch.setattr(store, "save_code", real)
        raise claims.Busy("Keeper away")

    monkeypatch.setattr(store, "save_code", flaky)
    approve = {"ticket": ticket, "decision": "approve"}
    first = c.post("/oauth/authorize/approve", data=approve, follow_redirects=False)
    assert first.status_code == 503 and "submit your choice again" in first.text
    again = c.post("/oauth/authorize/approve", data=approve, follow_redirects=False)
    assert again.status_code == 302 and "code=" in again.headers["location"]


def test_wrong_codes_sent_at_once_are_held_to_the_limit(_world, monkeypatch) -> None:
    # Each guess is counted before its lookup: a burst cannot all pass the
    # first count and only then be recorded.
    looked: list[str] = []

    async def lookup(code_id: str) -> None:
        looked.append(code_id)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(_world["store"], "device_by_user_code", lookup)

    async def burst() -> list[Any]:
        guesses = [device._look_up_counted(f"g{i}", "203.0.113.9") for i in range(20)]
        return await asyncio.gather(*guesses, return_exceptions=True)

    outcomes = asyncio.run(burst())
    assert len(looked) <= 10
    assert sum(isinstance(o, device._TooManyWrong) for o in outcomes) >= 10


def test_a_taken_over_refresh_the_idp_refuses_is_named_as_loom_s_loss(sso, monkeypatch) -> None:
    # The dead process may have rotated the IdP's token: the session ends, but
    # the person is not told the identity provider ended it.
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sso_round_trip(c, grant["user_code"]).text)
    rt = _poll(c, grant["device_code"]).json()["refresh_token"]
    store = oauth.store
    family = store.refresh[oauth.token_id(rt)]["family"]
    started = int(time.time())
    store.keeper[f"family:{family}"] = f"0:spending:{started - 600}:n"  # died long ago
    sso["upstream"] = "refused"
    r = _refresh(c, rt)
    assert r.status_code == 400 and "did not finish" in r.json()["error_description"]


def test_a_logout_with_a_refused_sessions_token_still_ends_it(_world, sso, monkeypatch) -> None:
    # A refusal retires the session key without always ending the session
    # (SSO turned off): the logout that follows must end it, not just answer 200.
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sso_round_trip(c, grant["user_code"]).text)
    rt = _poll(c, grant["device_code"]).json()["refresh_token"]
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "password")
    assert _refresh(c, rt).status_code == 400  # "single sign-on is off here"
    family = oauth.store.refresh[oauth.token_id(rt)]["family"]
    r = c.post("/oauth/revoke", data={"token": rt, "client_id": device.DEVICE_CLIENT_ID})
    assert r.status_code == 200 and family in _world["revoked"]


def test_a_retried_logout_is_confirmed_without_ending_the_session_again(_world) -> None:
    c = _client()
    tokens = _signed_in_tokens(c)
    for _ in range(3):  # `loom logout` retried
        r = c.post(
            "/oauth/revoke", data={"token": tokens["refresh_token"], "client_id": "loom-cli"}
        )
        assert r.status_code == 200
    reasons = [kw["subject"]["reason"] for n, kw in _world["events"] if n == "auth.session_revoked"]
    assert reasons == ["logout"]


def test_an_old_copy_logging_out_an_ended_session_only_confirms(_world) -> None:
    c = _client()
    old = _signed_in_tokens(c)["refresh_token"]
    new = _refresh(c, old).json()["refresh_token"]  # rotated: `old` is a stale copy now
    c.post("/oauth/revoke", data={"token": new, "client_id": "loom-cli"})
    for _ in range(2):  # a second terminal still holding the old token logs out
        r = c.post("/oauth/revoke", data={"token": old, "client_id": "loom-cli"})
        assert r.status_code == 200
    reasons = [kw["subject"]["reason"] for n, kw in _world["events"] if n == "auth.session_revoked"]
    assert reasons == ["logout"]


@pytest.mark.parametrize("ended_by", ["old copy's logout", "replay"])
def test_a_current_token_logging_out_an_ended_session_keeps_its_first_reason(
    _world, ended_by: str
) -> None:
    c = _client()
    old = _signed_in_tokens(c)["refresh_token"]
    new = _refresh(c, old).json()["refresh_token"]
    if ended_by == "replay":
        _refresh(c, old)  # a stolen copy turns up: the session ends as replayed
        first = "refresh token replayed"
    else:
        c.post("/oauth/revoke", data={"token": old, "client_id": "loom-cli"})
        first = "logout (token already spent)"
    assert c.post("/oauth/revoke", data={"token": new, "client_id": "loom-cli"}).status_code == 200
    reasons = [kw["subject"]["reason"] for n, kw in _world["events"] if n == "auth.session_revoked"]
    assert reasons == [first]  # the theft's reason is not overwritten by a logout


@pytest.mark.parametrize("ended_by", ["old copy's logout", "replay"])
def test_logging_out_an_ended_session_revokes_the_idp_s_current_token(
    _world, sso, monkeypatch, ended_by: str
) -> None:
    revoked: list[str] = []

    async def idp_revoke(token: str) -> None:
        revoked.append(token)

    monkeypatch.setattr(oidc, "revoke", idp_revoke)
    c = _client()
    grant = _start(c)
    _approve(c, grant["user_code"], _sso_round_trip(c, grant["user_code"]).text)
    old = _poll(c, grant["device_code"]).json()["refresh_token"]  # holds idp-r1
    new = _refresh(c, old).json()["refresh_token"]  # holds idp-r1+, the IdP's current
    if ended_by == "replay":
        _refresh(c, old)
    else:
        c.post("/oauth/revoke", data={"token": old, "client_id": device.DEVICE_CLIENT_ID})
    c.post("/oauth/revoke", data={"token": new, "client_id": device.DEVICE_CLIENT_ID})
    assert "idp-r1+" in revoked


# What a browser sends when the user submits a form on this server's own page.
_FORM_SUBMIT = {
    "origin": "http://testserver",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}


def _sign_in_routes_behind_the_gate(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The sign-in routes behind the cross-site gate, with auth on."""
    from server.main import attach_browser_middleware

    monkeypatch.setattr(oauth.settings, "auth_required", True)
    monkeypatch.setattr(oauth.settings, "identity_provider", "gateway")
    monkeypatch.setattr(oauth.settings, "public_base_url", "")
    monkeypatch.setattr(oauth.settings, "cors_origins", "")
    app = oauth_app()
    attach_browser_middleware(app)
    return TestClient(app)


def _authorize_params(c: TestClient) -> dict[str, str]:
    reg = c.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1:9/cb"]}).json()
    return {
        "client_id": reg["client_id"],
        "redirect_uri": "http://127.0.0.1:9/cb",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }


# Each opens one server-rendered page that holds a sign-in form.
def _device_code_page(c: TestClient, request: pytest.FixtureRequest) -> httpx.Response:
    return c.get("/oauth/device", params={"user_code": _start(c)["user_code"]})


def _device_approval_page(c: TestClient, request: pytest.FixtureRequest) -> httpx.Response:
    return _sign_in(c, _start(c)["user_code"])


def _connect_sign_in_page(c: TestClient, request: pytest.FixtureRequest) -> httpx.Response:
    return c.get("/oauth/authorize", params=_authorize_params(c))


def _connect_approval_page(c: TestClient, request: pytest.FixtureRequest) -> httpx.Response:
    request.getfixturevalue("sso")
    r = c.get("/oauth/authorize", params=_authorize_params(c), follow_redirects=False)
    state = r.headers["location"].split("state=")[1]
    return c.get("/oauth/callback", params={"code": "idp-code", "state": state})


def _account_link_page(c: TestClient, request: pytest.FixtureRequest) -> httpx.Response:
    request.getfixturevalue("bobs_password_account")
    return _sso_round_trip(c, _start(c)["user_code"])


def _set_password_page(c: TestClient, request: pytest.FixtureRequest) -> httpx.Response:
    async def held(code: str) -> dict[str, Any]:
        return {"kind": "set_password"}

    request.getfixturevalue("monkeypatch").setattr(password_link, "_held", held)
    return c.get("/oauth/set-password", params={"code": "c"})


@pytest.mark.parametrize(
    ("open_page", "action"),
    [
        (_device_code_page, "/oauth/device"),
        (_device_approval_page, "/oauth/device/approve"),
        (_connect_sign_in_page, "/oauth/authorize"),
        (_connect_approval_page, "/oauth/authorize/approve"),
        (_account_link_page, "/oauth/link"),
        (_set_password_page, "/oauth/set-password"),
    ],
)
def test_a_sign_in_form_posts_with_its_pages_own_origin(
    monkeypatch,
    request,
    open_page: Callable[[TestClient, pytest.FixtureRequest], httpx.Response],
    action: str,
) -> None:
    # A browser serializes a POST's Origin as `null` when the page's policy is no-referrer,
    # and the gate refuses that; under same-origin it sends the server's own.
    c = _sign_in_routes_behind_the_gate(monkeypatch)
    r = open_page(c, request)
    assert r.status_code == 200, r.text
    assert f'<form method="post" action="{action}">' in r.text
    assert r.headers["referrer-policy"] == "same-origin"
    own = c.post(action, data={}, headers=_FORM_SUBMIT)
    assert own.status_code == 400, own.text  # the route itself answers: no fields
    refused = c.post(action, data={}, headers={**_FORM_SUBMIT, "origin": "null"})
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "CROSS_SITE_REQUEST"
