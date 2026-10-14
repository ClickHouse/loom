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

pytestmark = pytest.mark.usefixtures("ended_grant_claims", "no_restricted_grants", "no_keeper")


@pytest.fixture
def no_keeper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exchanges run unclaimed, as on a server without a Keeper; the claim
    itself is tested in test_oauth_claims.py."""
    from server.auth import oauth_claims

    async def _unclaimed(ch: object, key: str) -> tuple[oauth_claims.Claim, str]:
        return oauth_claims.Claim.UNCLAIMED, ""

    monkeypatch.setattr(oauth_claims, "claim", _unclaimed)


@pytest.fixture
def no_restricted_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The restricted-grant read a refresh makes; a test that needs one patches it."""

    async def _none(subject: str, org: str, namespaces: list[str], **_k: object) -> list[str]:
        return list(namespaces)

    monkeypatch.setattr(login, "widen_with_restricted_grants", _none)


def _revoke_subject(monkeypatch: pytest.MonkeyPatch, cutoff: int | None) -> None:
    """`loom revoke-token --subject`, as the exchange reads it from ClickHouse:
    every token, or those issued before `cutoff`."""
    from server.auth import revocation

    async def _revoked(subject: str, *, issued_at: int) -> bool:
        return cutoff is None or issued_at < cutoff

    monkeypatch.setattr(revocation, "subject_revoked", _revoked)


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
    c = _client()
    rt = _refresh_token_for_alice(c)
    _revoke_subject(monkeypatch, int(time.time()) + 5)
    r = _refresh(c, rt)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"


def test_refresh_refused_for_fully_revoked_subject(monkeypatch) -> None:
    c = _client()
    rt = _refresh_token_for_alice(c)
    _revoke_subject(monkeypatch, None)
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


@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
def test_a_failed_revocation_read_leaves_the_credential_for_the_retry(
    monkeypatch: pytest.MonkeyPatch, grant_type: str
) -> None:
    """A 503 says retry, so the code or refresh token must still work once
    the store is back; the exchange that succeeds uses it up."""
    from server.auth import revocation

    async def _down(subject: str, *, issued_at: int) -> bool:
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
        outage.setattr(revocation, "subject_revoked", _down)
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


def test_refresh_drops_a_namespace_whose_grant_ended_for_good(
    ended_grant_claims: dict[str, int],
) -> None:
    """A refresh mints with a fresh iat, past the cutoff a revoked grant left;
    the namespace leaves the sign-in snapshot, so no later refresh brings it back."""
    from server.auth.jwt import verify_token

    c = _client()
    code, verifier = _seed_code()
    oauth._store.codes[oauth._token_id(code)]["namespaces"] = ["alice-ns", "team"]
    rt = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://127.0.0.1:9999/cb",
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    ).json()["refresh_token"]
    ended_grant_claims["team"] = int(time.time()) + 5
    first = _refresh(c, rt).json()
    assert verify_token(first["access_token"]).namespaces == ("alice-ns",)
    # The rotated grant is newer than the cutoff now; only the snapshot keeps it out.
    ended_grant_claims["team"] = int(time.time()) - 3600
    second = _refresh(c, first["refresh_token"]).json()
    assert verify_token(second["access_token"]).namespaces == ("alice-ns",)


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


@pytest.mark.parametrize(("cut_after_authorize", "kept"), [(True, False), (False, True)])
def test_a_code_exchange_drops_a_namespace_taken_away_since_authorize(
    ended_grant_claims: dict[str, int], cut_after_authorize: bool, kept: bool
) -> None:
    """A cutoff after the code was issued drops the namespace; an older one,
    which the authorize step already applied, does not."""
    from server.auth.jwt import verify_token

    c = _client()
    code, verifier = _seed_code()
    oauth._store.codes[oauth._token_id(code)]["namespaces"] = ["alice-ns", "team"]
    ended_grant_claims["team"] = int(time.time()) + (5 if cut_after_authorize else -3600)
    tok = _exchange(c, code, verifier).json()
    want = ("alice-ns", "team") if kept else ("alice-ns",)
    assert verify_token(tok["access_token"]).namespaces == want


def test_a_code_exchange_is_refused_for_a_subject_revoked_since(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import revocation

    async def _revoked(subject: str, *, issued_at: int) -> bool:
        return True

    monkeypatch.setattr(revocation, "subject_revoked", _revoked)
    c = _client()
    code, verifier = _seed_code()
    r = _exchange(c, code, verifier)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"


def test_an_unreadable_revocation_store_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import revocation

    async def _down(subject: str, *, issued_at: int) -> bool:
        raise RuntimeError("replica away")

    monkeypatch.setattr(revocation, "subject_revoked", _down)
    c = _client()
    code, verifier = _seed_code()
    r = _exchange(c, code, verifier)
    assert r.status_code == 503 and r.json()["detail"]["error"] == "temporarily_unavailable"


def test_a_refresh_keeps_a_cut_restricted_namespace_still_granted(
    ended_grant_claims: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restricted namespace is read only by a token naming it; lowered to a
    viewer grant, the person still reads it, so the refresh keeps it."""
    from server.auth.jwt import verify_token

    async def _still_granted(
        subject: str, org: str, namespaces: list[str], **_k: object
    ) -> list[str]:
        return [*namespaces, "restricted-qa"]

    monkeypatch.setattr(login, "widen_with_restricted_grants", _still_granted)
    c = _client()
    code, verifier = _seed_code()
    oauth._store.codes[oauth._token_id(code)]["namespaces"] = ["alice-ns", "restricted-qa"]
    rt = _exchange(c, code, verifier).json()["refresh_token"]
    ended_grant_claims["restricted-qa"] = int(time.time()) + 5
    tok = _refresh(c, rt).json()
    assert verify_token(tok["access_token"]).namespaces == ("alice-ns", "restricted-qa")


def test_a_failed_restricted_grant_read_does_not_narrow_the_session(
    ended_grant_claims: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup that failed is not a grant that ended: the refresh answers
    503 and keeps the restricted namespace once the lookup works again."""
    from server.auth.jwt import verify_token

    calls = {"n": 0}

    async def _flaky(subject: str, org: str, namespaces: list[str], **kw: object) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            assert kw.get("strict") is True
            raise RuntimeError("replica away")
        return [*namespaces, "restricted-qa"]

    monkeypatch.setattr(login, "widen_with_restricted_grants", _flaky)
    c = _client()
    code, verifier = _seed_code()
    oauth._store.codes[oauth._token_id(code)]["namespaces"] = ["alice-ns", "restricted-qa"]
    rt = _exchange(c, code, verifier).json()["refresh_token"]
    ended_grant_claims["restricted-qa"] = int(time.time()) + 5
    assert _refresh(c, rt).status_code == 503
    tok = _refresh(c, rt).json()
    assert verify_token(tok["access_token"]).namespaces == ("alice-ns", "restricted-qa")


def test_a_concurrent_exchange_of_a_claimed_credential_is_told_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another exchange of the code holds its Keeper claim: this one may be
    the owner retrying, so it gets a retryable 503, never a final
    invalid_grant, and the code is left for the exchange that holds it."""
    from server.auth import oauth_claims

    async def _taken(ch: object, key: str) -> tuple[oauth_claims.Claim, str]:
        return oauth_claims.Claim.TAKEN, ""

    c = _client()
    code, verifier = _seed_code()
    with monkeypatch.context() as racing:
        racing.setattr(oauth_claims, "claim", _taken)
        refused = _exchange(c, code, verifier)
    assert refused.status_code == 503 and refused.headers["retry-after"] == "5"
    assert _exchange(c, code, verifier).status_code == 200


def test_an_exchange_past_its_bound_is_told_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exchange is bounded below the age its claim is taken over at, so a
    slow one never runs beside a taker; the code is left for the retry."""
    import asyncio

    from server.auth import oauth_claims, revocation

    async def _slow(subject: str, *, issued_at: int) -> bool:
        await asyncio.sleep(1)
        return False

    c = _client()
    code, verifier = _seed_code()
    with monkeypatch.context() as slow:
        slow.setattr(oauth_claims, "EXCHANGE_TIMEOUT_S", 0.05)
        slow.setattr(revocation, "subject_revoked", _slow)
        assert _exchange(c, code, verifier).status_code == 503
    assert _exchange(c, code, verifier).status_code == 200


def test_a_slow_consume_that_landed_is_never_answered_with_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consume has its own bound, not what is left of the decision's, so
    a consume that wrote its row and replied late still answers with the
    tokens."""
    import asyncio

    from server.auth import oauth_claims

    store = oauth._store
    consume = store.consume_code

    async def _slow(code: str, grant: dict) -> None:
        await consume(code, grant)
        await asyncio.sleep(0.3)

    monkeypatch.setattr(oauth_claims, "EXCHANGE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(store, "consume_code", _slow)
    c = _client()
    code, verifier = _seed_code()
    assert _exchange(c, code, verifier).status_code == 200


def test_a_find_past_its_bound_is_told_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credential read is inside the exchange's deadline: a find that
    stalls (its retries, a pool wait) cannot carry a live exchange past the
    age its claim is taken over at."""
    import asyncio

    from server.auth import oauth_claims

    store = oauth._store
    find = store.find_code

    async def _slow(code: str) -> dict | None:
        await asyncio.sleep(1)
        return await find(code)

    c = _client()
    code, verifier = _seed_code()
    with monkeypatch.context() as slow:
        slow.setattr(oauth_claims, "EXCHANGE_TIMEOUT_S", 0.05)
        slow.setattr(store, "find_code", _slow)
        assert _exchange(c, code, verifier).status_code == 503
    assert _exchange(c, code, verifier).status_code == 200


@pytest.fixture
def held_claims(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every exchange holds its claim; the keys released are recorded."""
    from server.auth import oauth_claims

    released: list[str] = []

    async def _held(ch: object, key: str) -> tuple[oauth_claims.Claim, str]:
        return oauth_claims.Claim.HELD, "me"

    async def _release(ch: object, key: str, holder: str) -> None:
        released.append(key)

    monkeypatch.setattr(oauth_claims, "claim", _held)
    monkeypatch.setattr(oauth_claims, "release", _release)
    return released


def test_a_decided_exchange_releases_its_claim(held_claims: list[str]) -> None:
    c = _client()
    code, verifier = _seed_code()
    assert _exchange(c, code, verifier).status_code == 200
    assert held_claims == [f"code:{oauth._token_id(code)}"]


@pytest.mark.parametrize("outcome", ["issued", "refused"])
def test_a_consume_that_did_not_finish_keeps_the_claim(
    held_claims: list[str], monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """A consume that failed may still land, so the claim is kept until it
    goes stale and no retry reads the credential meanwhile. A refusal still
    answers invalid_grant; an issue answers 503, never a 500."""
    store = oauth._store

    async def _down(code: str, grant: dict) -> None:
        raise RuntimeError("replica away")

    monkeypatch.setattr(store, "consume_code", _down)
    c = _client()
    code, verifier = _seed_code()
    if outcome == "issued":
        r = _exchange(c, code, verifier)
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
    else:
        r = _exchange(c, code, "not-the-verifier")
        assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"
    assert held_claims == []


@pytest.mark.parametrize("grant_type", ["authorization_code", "refresh_token"])
def test_a_failed_token_issue_leaves_the_credential_for_the_retry(
    monkeypatch: pytest.MonkeyPatch, grant_type: str
) -> None:
    """The new refresh token is saved before the old credential is used up,
    so a save that fails answers 503 and the retry gets its tokens."""
    store = oauth._store
    c = _client()
    code, verifier = _seed_code()
    if grant_type == "authorization_code":

        def exchange():
            return _exchange(c, code, verifier)
    else:
        rt = _exchange(c, code, verifier).json()["refresh_token"]

        def exchange():
            return _refresh(c, rt)

    save = store.save_refresh

    async def _down(token: str, grant: dict) -> None:
        raise RuntimeError("replica away")

    monkeypatch.setattr(store, "save_refresh", _down)
    assert exchange().status_code == 503
    monkeypatch.setattr(store, "save_refresh", save)
    assert exchange().status_code == 200
