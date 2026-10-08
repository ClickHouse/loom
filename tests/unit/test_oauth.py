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
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import login, oauth
from tests.unit.oauth_store_fake import MemoryOAuthStore


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
    monkeypatch.setattr(oauth, "_store", MemoryOAuthStore())

    async def active(subject: str, org: str, roles: list[str]) -> list[str]:
        return roles

    monkeypatch.setattr(login, "refreshed_roles", active)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(oauth.router)
    return TestClient(app)


# ── pure helpers ──────────────────────────────────────────────────────────
def test_verify_pkce_s256() -> None:
    v = "a-high-entropy-verifier-string-1234567890"
    assert oauth.verify_pkce_s256(v, _challenge(v)) is True
    assert oauth.verify_pkce_s256("wrong", _challenge(v)) is False
    assert oauth.verify_pkce_s256("", _challenge(v)) is False
    assert oauth.verify_pkce_s256(v, "") is False


def test_discovery_metadata_shape() -> None:
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
    code = "test-code-123"
    grant = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": _challenge(verifier),
        "subject": "alice@example.com",
        "org": "test-org",
        "namespaces": ["alice-ns"],
        "roles": ["writer"],
        "exp": int(time.time()) + 60,
    }
    asyncio.run(oauth._store.save_code(code, grant))
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
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"


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
    # Old refresh token is rotated out (single-use).
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
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"
    r = _refresh(c, rt)
    assert r.status_code == 200 and r.json()["access_token"]


def test_refresh_without_client_id_is_refused_unconsumed() -> None:
    """A public client names itself on refresh; a refresh that does not is
    refused before the token is read, so the rightful client still refreshes."""
    c = _client()
    rt = _refresh_token_for_alice(c)
    r = _refresh(c, rt, client_id="")
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_request"
    assert _refresh(c, rt).status_code == 200


def test_store_keeps_token_hashes_not_tokens() -> None:
    """Neither the code nor the refresh token is readable from the store: a
    leaked oauth_state row must not be a working credential."""
    c = _client()
    store = oauth._store
    code, _ = _seed_code()
    assert list(store.codes) == [hashlib.sha256(code.encode()).hexdigest()]
    rt = _refresh_token_for_alice(c)
    assert list(store.refresh) == [hashlib.sha256(rt.encode()).hexdigest()]


class _FakeCH:
    """Just enough of the CH client for _CHStore: inserts append versions,
    query_one answers the FINAL + consumed = 0 read of one (record_type, id)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def insert(self, table: str, rows: list[dict]) -> None:
        self.rows.extend(dict(r) for r in rows)

    async def query_one(self, sql: str, params: dict | None = None) -> dict | None:
        p = params or {}
        versions = [
            r for r in self.rows if r["record_type"] == p.get("t") and r["id"] == p.get("id")
        ]
        if not versions:
            return None
        last = max(versions, key=lambda r: r["version"])
        return None if last.get("consumed", 0) else last


def _ch_store(monkeypatch) -> _FakeCH:
    ch = _FakeCH()
    store = oauth._CHStore()
    monkeypatch.setattr(store, "_ch", lambda: ch)
    monkeypatch.setattr(oauth, "_store", store)
    return ch


def test_ch_store_writes_token_hashes(monkeypatch) -> None:
    ch = _ch_store(monkeypatch)
    c = _client()
    code, verifier = _seed_code()
    tok = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    ).json()
    rt = tok["refresh_token"]
    ids = {r["id"] for r in ch.rows}
    assert code not in ids and rt not in ids
    assert hashlib.sha256(rt.encode()).hexdigest() in ids
    assert _refresh(c, rt).status_code == 200


def test_refresh_refused_for_subject_revoked_after_issue(monkeypatch) -> None:
    """`revoke-token --subject S --issued-before T` must not be undone by one
    refresh: a refresh token minted before T can no longer mint."""
    from server.auth import revocation

    c = _client()
    rt = _refresh_token_for_alice(c)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {"alice@example.com": int(time.time()) + 5})
    r = _refresh(c, rt)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"


def test_refresh_refused_for_fully_revoked_subject(monkeypatch) -> None:
    from server.auth import revocation

    c = _client()
    rt = _refresh_token_for_alice(c)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({"alice@example.com"}))
    monkeypatch.setattr(revocation, "_revoked_before", {})
    r = _refresh(c, rt)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"


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


def test_refresh_waits_for_revocation_snapshot(monkeypatch) -> None:
    """Before the revocation list has loaded, refresh refuses retryably and
    does not burn the refresh token."""
    from server.auth import revocation

    c = _client()
    rt = _refresh_token_for_alice(c)
    monkeypatch.setattr(revocation, "_loaded", False)
    assert _refresh(c, rt).status_code == 503
    monkeypatch.setattr(revocation, "_loaded", True)
    assert _refresh(c, rt).status_code == 200


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
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_redirect_uri"


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
        oauth._store.codes[oauth._token_id(code)]["roles"] = roles
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
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"


def test_refresh_ends_when_the_account_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    async def gone(subject: str, org: str, roles: list[str]) -> None:
        return None

    monkeypatch.setattr(login, "refreshed_roles", gone)
    c = _client()
    rt = _refresh_token(c)
    r = _refresh(c, rt)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"


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
    def __init__(self, row: dict[str, str] | None) -> None:
        self.row = row

    async def query_one(self, sql: str, params: object = None) -> dict[str, str] | None:
        return self.row


@pytest.mark.parametrize(
    ("row", "roles", "want"),
    [
        (None, ["writer"], None),
        ({"contact_email": "a@example.com"}, ["writer"], ["writer"]),
        ({"contact_email": "host@example.com"}, ["platform_admin"], ["platform_admin"]),
        ({"contact_email": "gone@example.com"}, ["platform_admin"], ["writer"]),
        ({"contact_email": "host@example.com"}, ["writer"], ["writer"]),  # never promotes
    ],
)
async def test_refreshed_roles(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, str] | None, roles: list[str], want: object
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
    assert r.json()["detail"]["error"] == "invalid_client_metadata"


@pytest.mark.parametrize("path", ["/oauth/token", "/oauth/authorize"])
def test_a_non_utf8_form_body_is_a_400_not_a_500(path: str) -> None:
    r = _client().post(
        path,
        content=b"grant_type=\xff\xfe",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_request"
