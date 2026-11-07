"""Unit coverage for the OAuth 2.1 AS core (flag-gated MCP one-click connect).

Exercises the security-critical pure logic + the code/refresh token exchange
through the ASGI app with a seeded in-process store — no ClickHouse, no live
server. The browser login/consent step (resolve_user_credentials → CH) is
covered separately; here we seed an authorized code directly.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.auth import login, oauth
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app

pytestmark = pytest.mark.usefixtures("subjects_not_revoked", "auth_attempts")


def _revoke_subject(monkeypatch: pytest.MonkeyPatch, cutoff: int | None) -> None:
    """`loom revoke-token --subject`, as the exchange reads it from ClickHouse:
    every token, or those issued before `cutoff`."""
    from server.auth import revocation

    async def _revoked(subjects: list[str], *, issued_at: int) -> bool:
        return cutoff is None or issued_at < cutoff

    monkeypatch.setattr(revocation, "subjects_revoked", _revoked)


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


@pytest.fixture(autouse=True)
def _mem_store(monkeypatch):
    """Swap the default CH-backed store for the in-memory one (no live CH in
    unit tests). The router reads the module global per call, so this applies.
    A refresh re-checks the account in CH; here it is active and keeps its
    roles unless a test says otherwise."""
    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())

    async def active(subject: str, org: str, roles: list[str]) -> list[str]:
        return roles

    monkeypatch.setattr(login, "refreshed_roles", active)


def _client() -> TestClient:
    app = oauth_app()
    return TestClient(app)


# ── pure helpers ──────────────────────────────────────────────────────────
def test_verify_pkce_s256() -> None:
    v = "a-high-entropy-verifier-string-1234567890"
    assert oauth.verify_pkce_s256(v, _challenge(v)) is True
    assert oauth.verify_pkce_s256("wrong", _challenge(v)) is False
    assert oauth.verify_pkce_s256("", _challenge(v)) is False
    assert oauth.verify_pkce_s256(v, "") is False


def test_sign_in_metadata_without_mcp_connect(monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_enabled", False)
    md = oauth.authorization_server_metadata("https://loom.example")
    assert md["device_authorization_endpoint"] == "https://loom.example/oauth/device_authorization"
    assert md["revocation_endpoint"] == "https://loom.example/oauth/revoke"
    assert "authorization_endpoint" not in md
    assert "registration_endpoint" not in md
    assert "authorization_code" not in md["grant_types_supported"]


def test_discovery_metadata_shape(monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_enabled", True)
    md = oauth.authorization_server_metadata("https://loom.example")
    assert md["issuer"] == "https://loom.example"
    assert md["authorization_endpoint"].endswith("/oauth/authorize")
    assert md["token_endpoint"].endswith("/oauth/token")
    assert md["code_challenge_methods_supported"] == ["S256"]  # S256-only
    assert "authorization_code" in md["grant_types_supported"]
    assert "refresh_token" in md["grant_types_supported"]
    prm = oauth.protected_resource_metadata("https://loom.example", "https://loom.example/mcp")
    assert prm["authorization_servers"] == ["https://loom.example"]
    assert prm["resource"].endswith("/mcp")


def test_discovery_endpoints_served() -> None:
    c = _client()
    r = c.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200 and "token_endpoint" in r.json()
    r = c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200 and r.json()["authorization_servers"]


def test_dynamic_client_registration() -> None:
    c = _client()
    r = c.post(
        "/oauth/register",
        json={"redirect_uris": ["http://127.0.0.1:9999/cb"], "client_name": "Test"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"].startswith("loom-")
    assert body["token_endpoint_auth_method"] == "none"
    # Missing redirect_uris is rejected.
    assert c.post("/oauth/register", json={}).status_code == 400


def _seed_code(
    client_id: str = "loom-test", redirect_uri: str = "http://127.0.0.1:9999/cb"
) -> tuple[str, str]:
    """Seed an authorized code (as if consent succeeded). Returns (code, verifier)."""
    verifier = "verifier-" + base64.urlsafe_b64encode(b"x" * 24).decode()
    code = f"test-code-{secrets.token_hex(4)}"
    grant = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": _challenge(verifier),
        "subject": "alice@example.com",
        "org": "test-org",
        "namespaces": ["alice-ns"],
        "roles": ["writer"],
        "exp": int(time.time()) + 60,
        "since": int(time.time()),
        "family": f"f-{secrets.token_hex(4)}",
        "upstream_refresh": "",
    }
    asyncio.run(oauth.store.save_code(code, grant))
    return code, verifier


def test_authorization_code_exchange_succeeds() -> None:
    c = _client()
    code, verifier = _seed_code()
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["access_token"] and tok["refresh_token"]
    # Access token is a real loom JWT, subject-aligned to the user.
    from server.auth.jwt import verify_token

    info = verify_token(tok["access_token"])
    assert info.subject == "alice@example.com"
    assert "alice-ns" in info.namespaces


def test_code_is_single_use() -> None:
    c = _client()
    code, verifier = _seed_code()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/cb",
        "client_id": "loom-test",
        "code_verifier": verifier,
    }
    assert c.post("/oauth/token", data=data).status_code == 200
    # Second redemption of the same code is rejected.
    assert c.post("/oauth/token", data=data).status_code == 400


def test_pkce_mismatch_rejected() -> None:
    c = _client()
    code, _ = _seed_code()
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": "the-wrong-verifier",
        },
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_token_rotates() -> None:
    c = _client()
    code, verifier = _seed_code()
    first = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    ).json()
    rt = first["refresh_token"]
    r = _refresh(c, rt)
    assert r.status_code == 200 and r.json()["access_token"]
    # The old refresh token is rotated out (single-use).
    assert _refresh(c, rt).status_code == 400


def _refresh_token_for_alice(c: TestClient) -> str:
    code, verifier = _seed_code()
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    ).json()["refresh_token"]


def _refresh(c: TestClient, rt: str, client_id: str = "loom-test"):
    return c.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": client_id},
    )


def test_refresh_refused_for_another_client() -> None:
    """A refresh token only refreshes for the client it was issued to (RFC 6749
    §6); a mismatched client_id is refused without burning the token, so the
    rightful client can still refresh."""
    c = _client()
    rt = _refresh_token_for_alice(c)
    r = _refresh(c, rt, client_id="loom-other")
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    r = _refresh(c, rt)
    assert r.status_code == 200 and r.json()["access_token"]


def test_refresh_without_client_id_is_refused_unconsumed() -> None:
    """A public client names itself on refresh; a refresh that does not is
    refused before the token is read, so the rightful client still refreshes."""
    c = _client()
    rt = _refresh_token_for_alice(c)
    r = _refresh(c, rt, client_id="")
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"
    assert _refresh(c, rt).status_code == 200


def test_store_keeps_token_hashes_not_tokens() -> None:
    """Neither the code nor the refresh token is readable from the store: a
    leaked oauth_state row must not be a working credential."""
    c = _client()
    store = oauth.store
    code, _ = _seed_code()
    assert list(store.records("code")) == [hashlib.sha256(code.encode()).hexdigest()]
    rt = _refresh_token_for_alice(c)
    assert list(store.refresh) == [hashlib.sha256(rt.encode()).hexdigest()]
    assert not any(token in str(store.keeper) for token in (code, rt))


def test_refresh_refused_for_subject_revoked_after_issue(monkeypatch) -> None:
    """`revoke-token --subject S --issued-before T` must not be undone by one
    refresh: a refresh token minted before T can no longer mint."""
    c = _client()
    rt = _refresh_token_for_alice(c)
    _revoke_subject(monkeypatch, int(time.time()) + 5)
    r = _refresh(c, rt)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_refused_for_fully_revoked_subject(monkeypatch) -> None:
    c = _client()
    rt = _refresh_token_for_alice(c)
    _revoke_subject(monkeypatch, None)
    r = _refresh(c, rt)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_allowed_when_cutoff_predates_issue(monkeypatch) -> None:
    """A cutoff before the refresh token's mint time leaves it working (the
    re-login after the leak keeps its session)."""
    from server.auth import revocation

    c = _client()
    rt = _refresh_token_for_alice(c)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({"someone-else"}))
    monkeypatch.setattr(revocation, "_revoked_before", {"alice@example.com": int(time.time()) - 60})
    r = _refresh(c, rt)
    assert r.status_code == 200 and r.json()["access_token"]


@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
def test_a_failed_revocation_read_leaves_the_credential_for_the_retry(
    monkeypatch: pytest.MonkeyPatch, grant_type: str
) -> None:
    """A 503 says retry, so the code or refresh token must still work once
    the store is back; the exchange that succeeds uses it up."""
    from server.auth import revocation

    async def _down(subjects: list[str], *, issued_at: int) -> bool:
        raise RuntimeError("replica away")

    c = _client()
    code, verifier = _seed_code()
    if grant_type == "authorization_code":

        def exchange():
            return _exchange(c, code, verifier)
    else:
        rt = _exchange(c, code, verifier).json()["refresh_token"]

        def exchange():
            return _refresh(c, rt)

    with monkeypatch.context() as outage:
        outage.setattr(revocation, "subjects_revoked", _down)
        failed = exchange()
    assert failed.status_code == 503 and failed.headers["retry-after"] == "5"
    assert exchange().status_code == 200
    assert exchange().status_code == 400


def test_unsupported_grant_rejected() -> None:
    c = _client()
    assert c.post("/oauth/token", data={"grant_type": "password"}).status_code == 400


# ── redirect URIs, consent, refresh re-checks ─────────────────────────────
@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://claude.ai/api/mcp/auth_callback", True),
        ("http://127.0.0.1:9999/cb", True),
        ("http://localhost:33418/callback", True),
        ("http://[::1]:8080/cb", True),
        ("cursor://anysphere.cursor-retrieval/oauth/callback", True),
        ("http://evil.example/cb", False),
        ("https://ok.example/cb#frag", False),
        ("javascript:alert(1)", False),
        ("data:text/html,x", False),
        ("file:///etc/passwd", False),
        ("/relative/cb", False),
        ("https:///no-host", False),
        ("https://a.example/ cb", False),
        ("https://a.example/" + "x" * 2100, False),
    ],
)
def test_redirect_uri_policy(uri: str, ok: bool) -> None:
    assert oauth.valid_redirect_uri(uri) is ok


@pytest.mark.parametrize(
    "body",
    [
        {"redirect_uris": ["javascript:alert(1)"]},
        {"redirect_uris": ["https://ok.example/cb", "http://evil.example/cb"]},
        {"redirect_uris": "https://ok.example/cb"},
        {"redirect_uris": [f"https://ok.example/{i}" for i in range(11)]},
        {"redirect_uris": [7]},
        [],
    ],
)
def test_registration_refuses_unsafe_redirects(body: object) -> None:
    r = _client().post("/oauth/register", json=body)
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_consent_page_names_where_the_code_goes() -> None:
    c = _client()
    reg = c.post(
        "/oauth/register",
        json={"redirect_uris": ["https://collector.example/cb"], "client_name": "Claude\x00<b>"},
    ).json()
    page = c.get(
        "/oauth/authorize",
        params={
            "client_id": reg["client_id"],
            "redirect_uri": "https://collector.example/cb",
            "code_challenge": _challenge("v" * 43),
            "code_challenge_method": "S256",
        },
    ).text
    assert "<b>collector.example</b>" in page
    assert "Claude&lt;b&gt;" in page  # control chars dropped, markup escaped


def _refresh_token(c: TestClient, roles: list[str] | None = None) -> str:
    code, verifier = _seed_code()
    if roles is not None:
        oauth.store.change("code", roles=roles)
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    ).json()["refresh_token"]


def test_refresh_by_another_client_is_refused() -> None:
    c = _client()
    rt = _refresh_token(c)
    r = c.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": "loom-other"},
    )
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_ends_when_the_account_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def gone(subject: str, org: str, roles: list[str]) -> None:
        return None

    monkeypatch.setattr(login, "refreshed_roles", gone)
    c = _client()
    rt = _refresh_token(c)
    r = _refresh(c, rt)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_refresh_carries_the_re_derived_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth.jwt import verify_token

    async def demoted(subject: str, org: str, roles: list[str]) -> list[str]:
        return ["writer"]

    c = _client()
    rt = _refresh_token(c, roles=["platform_admin"])
    monkeypatch.setattr(login, "refreshed_roles", demoted)
    tok = _refresh(c, rt).json()
    assert [r.name for r in verify_token(tok["access_token"]).roles] == ["WRITER"]


class _RowCH:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row

    async def query_one(self, sql: str, params: object = None) -> dict[str, object] | None:
        return self.row


@pytest.mark.parametrize(
    ("row", "roles", "want"),
    [
        ({"accounts": 0, "verified_email": ""}, ["writer"], None),
        ({"accounts": 1, "verified_email": "a@example.com"}, ["writer"], ["writer"]),
        (
            {"accounts": 1, "verified_email": "host@example.com"},
            ["platform_admin"],
            ["platform_admin"],
        ),
        ({"accounts": 1, "verified_email": "gone@example.com"}, ["platform_admin"], ["writer"]),
        # never promotes
        ({"accounts": 1, "verified_email": "host@example.com"}, ["writer"], ["writer"]),
        # an unverified email (none read back) keeps no role
        ({"accounts": 1, "verified_email": ""}, ["platform_admin"], ["writer"]),
    ],
)
async def test_refreshed_roles(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, object] | None, roles: list[str], want: object
) -> None:
    from server.settings import settings

    monkeypatch.undo()  # the real refreshed_roles, not the autouse stand-in
    monkeypatch.setattr(settings, "platform_admins", "host@example.com")
    monkeypatch.setattr(login, "get_client", lambda: _RowCH(row))
    assert await login.refreshed_roles("u-host-1", "org", roles) == want


@pytest.mark.parametrize("body", [b"{not json", b"", b"\xff\xfe"])
def test_a_malformed_registration_body_is_a_400_not_a_500(body: bytes) -> None:
    r = _client().post(
        "/oauth/register", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_client_metadata"


@pytest.mark.parametrize("path", ["/oauth/token", "/oauth/authorize"])
def test_a_non_utf8_form_body_is_a_400_not_a_500(path: str) -> None:
    r = _client().post(
        path,
        content=b"grant_type=\xff\xfe",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_a_code_spend_lost_to_an_undone_change_is_a_retry_not_a_replay(monkeypatch) -> None:
    from server.auth import claims

    code, verifier = _seed_code()
    store = oauth.store
    real = store._swap

    async def swap(key: str, old: str | None, new: str, expires_at: int) -> str | None:
        # Another request's change lands first and is undone: not spent.
        store.keeper[key] = json.dumps(json.loads(store.keeper[key]) | {"nonce": "other"})
        monkeypatch.setattr(store, "_swap", real)
        return store.keeper[key]

    monkeypatch.setattr(store, "_swap", swap)
    with pytest.raises(claims.Changed):
        asyncio.run(store.take_code(code, "loom-test", lambda _g: True))
    assert asyncio.run(store.take_code(code, "loom-test", lambda _g: True)) is not None


def _exchange(c: TestClient, code: str, verifier: str):
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    )


def test_a_code_exchange_is_refused_for_a_subject_revoked_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import revocation

    async def _revoked(subjects: list[str], *, issued_at: int) -> bool:
        return True

    monkeypatch.setattr(revocation, "subjects_revoked", _revoked)
    code, verifier = _seed_code()
    r = _exchange(_client(), code, verifier)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_an_unreadable_revocation_store_mints_nothing_and_keeps_the_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import revocation

    real = revocation.subjects_revoked

    async def _down(subjects: list[str], *, issued_at: int) -> bool:
        raise RuntimeError("replica away")

    c = _client()
    code, verifier = _seed_code()
    with monkeypatch.context() as down:
        down.setattr(revocation, "subjects_revoked", _down)
        r = _exchange(c, code, verifier)
    assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"
    monkeypatch.setattr(revocation, "subjects_revoked", real)
    assert _exchange(c, code, verifier).status_code == 200  # put back for the retry


@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
def test_an_exchange_issues_its_tokens_at_the_time_before_its_revocation_read(
    monkeypatch: pytest.MonkeyPatch, grant_type: str
) -> None:
    """A subject revocation can land while the exchange runs, after its read:
    the tokens carry the time taken before the read, so it still postdates
    them and cuts them."""
    import jwt as pyjwt

    from server.auth import revocation

    c = _client()
    code, verifier = _seed_code()
    if grant_type == "refresh_token":
        rt = _exchange(c, code, verifier).json()["refresh_token"]
    before = int(time.time())
    now = {"t": float(before)}
    monkeypatch.setattr(time, "time", lambda: now["t"])

    async def _slow(subjects: list[str], *, issued_at: int) -> bool:
        now["t"] += 100  # the read takes 100 s on the fake clock
        return False

    monkeypatch.setattr(revocation, "subjects_revoked", _slow)
    monkeypatch.setattr(oauth.settings, "oauth_access_token_ttl_seconds", 1800)
    r = _exchange(c, code, verifier) if grant_type == "authorization_code" else _refresh(c, rt)
    tok = r.json()
    # Unverified: a verify would refuse an iat past the real clock first.
    claims = pyjwt.decode(tok["access_token"], options={"verify_signature": False})
    assert claims["iat"] == before
    assert claims["exp"] - claims["iat"] == tok["expires_in"] == 1800
    saved = oauth.store.refresh[oauth.token_id(tok["refresh_token"])]
    assert int(saved["exp"]) - oauth._REFRESH_TTL == before


def test_a_read_behind_keeper_is_a_retry_and_a_later_generation_a_replay(monkeypatch) -> None:
    from server.auth import claims

    c = _client()
    rt = _refresh(c, _refresh_token_for_alice(c)).json()["refresh_token"]  # generation 1
    store = oauth.store
    grant = store.refresh[oauth.token_id(rt)]
    assert grant["generation"] == 1
    key = f"family:{grant['family']}"
    real = store._swap

    async def behind(k: str, old: str | None, new: str, exp: int) -> str | None:
        return f"0:{'0' * 64}"  # generation 0: what a read behind the rotation shows

    monkeypatch.setattr(store, "_swap", behind)
    with pytest.raises(claims.Busy):
        asyncio.run(store.take_refresh(rt, "loom-test"))

    async def ahead(k: str, old: str | None, new: str, exp: int) -> str | None:
        return f"{grant.get('generation', 0) + 1}:{'1' * 64}"

    monkeypatch.setattr(store, "_swap", ahead)
    with pytest.raises(oauth.CodeReplayed):
        asyncio.run(store.take_refresh(rt, "loom-test"))
    monkeypatch.setattr(store, "_swap", real)
    assert key in store.keeper


def test_a_spend_abandoned_long_ago_is_taken_over(monkeypatch) -> None:
    from server.auth import claims

    rt = _refresh_token_for_alice(_client())
    store = oauth.store
    grant = store.refresh[oauth.token_id(rt)]
    key = f"family:{grant['family']}"
    started = int(time.time())
    store.keeper[key] = f"0:spending:{started}:n"  # its process died mid-refresh
    with pytest.raises(claims.Busy):  # still in time to finish
        asyncio.run(store.take_refresh(rt, "loom-test"))
    monkeypatch.setattr(time, "time", lambda: started + 600)
    # It never saved a next token, so none was handed out: the session goes on.
    r = _refresh(_client(), rt)
    assert r.status_code == 200 and r.json()["refresh_token"] != rt


def test_an_older_generations_abandoned_spend_seen_by_a_read_behind_is_a_retry(
    monkeypatch,
) -> None:
    from server.auth import claims

    c = _client()
    rt = _refresh(c, _refresh_token_for_alice(c)).json()["refresh_token"]  # generation 1
    store = oauth.store

    async def behind(k: str, old: str | None, new: str, exp: int) -> str | None:
        return f"0:spending:{int(time.time()) - 600}:n"  # generation 0's, long ago

    monkeypatch.setattr(store, "_swap", behind)
    with pytest.raises(claims.Busy):
        asyncio.run(store.take_refresh(rt, "loom-test"))


def test_a_refresh_cancelled_midway_leaves_the_token_usable(monkeypatch) -> None:
    c = _client()
    rt = _refresh_token_for_alice(c)

    async def cancelled(subject: str, org: str, roles: list[str]) -> list[str]:
        raise asyncio.CancelledError  # the server shuts down while the refresh runs

    form = {"grant_type": "refresh_token", "refresh_token": rt, "client_id": "loom-test"}
    with monkeypatch.context() as gone:
        gone.setattr(login, "refreshed_roles", cancelled)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(oauth._refresh_token_grant(form))
    assert _refresh(c, rt).status_code == 200


async def test_a_put_back_that_fails_still_lets_the_cancellation_through() -> None:
    async def fails() -> None:
        raise RuntimeError("Keeper away")

    async def refresh() -> None:
        try:
            raise asyncio.CancelledError
        except asyncio.CancelledError:
            await oauth.put_back(fails())
            raise

    with pytest.raises(asyncio.CancelledError):
        await refresh()


async def test_a_put_back_finishes_though_the_request_is_cancelled_again() -> None:
    saved = asyncio.Event()

    async def slow_save() -> None:
        await asyncio.sleep(0.05)
        saved.set()

    request = asyncio.create_task(oauth.put_back(slow_save()))
    await asyncio.sleep(0)
    request.cancel()  # the shutdown cancels again while the put-back runs
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.wait_for(saved.wait(), 1)


async def test_shutdown_lands_a_put_back_its_cancelled_request_left_running() -> None:
    from server.core import shielded

    saved = asyncio.Event()

    async def slow_save() -> None:
        await asyncio.sleep(0.05)
        saved.set()

    request = asyncio.create_task(oauth.put_back(slow_save()))
    await asyncio.sleep(0)
    request.cancel()  # cancelled again: the wait returns, the save runs on
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not saved.is_set()
    await shielded.drain(timeout_s=1)  # what shutdown does before ClickHouse goes
    assert saved.is_set()


def test_a_refused_refresh_presented_again_is_no_theft(monkeypatch) -> None:
    # A client that asks twice (the SDK retries once) after an honest refusal
    # must not be told a copy is in other hands, nor have its session ended for it.
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, **kw: Any) -> str:
        events.append((event_type, kw))
        return ""

    monkeypatch.setattr(oauth.audit, "emit", emit)
    c = _client()
    rt = _refresh_token_for_alice(c)
    _revoke_subject(monkeypatch, None)
    first, again = _refresh(c, rt), _refresh(c, rt)
    assert first.status_code == again.status_code == 400
    assert "presented again" not in again.json().get("error_description", "")
    assert not [
        kw for name, kw in events if kw.get("subject", {}).get("reason") == "refresh token replayed"
    ]


def test_every_token_answer_says_this_build_keeps_sessions() -> None:
    # The SDK takes a refusal without it for a previous build's, mid-upgrade.
    from types import SimpleNamespace

    c = _client()
    answers = [
        _refresh(c, _refresh_token_for_alice(c)),
        _refresh(c, "not-a-token"),
        c.post("/oauth/token", data={"grant_type": "password"}),
        oauth._busy_response(SimpleNamespace(url=SimpleNamespace(path="/oauth/token")), OSError()),
    ]
    assert [r.status_code for r in answers] == [200, 400, 400, 503]
    assert all(r.headers.get(oauth.SESSIONS_HEADER) == "1" for r in answers)
