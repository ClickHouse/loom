"""OIDC login trusts only what the configured IdP vouches for.

The discovery document must name the configured issuer, `email_verified`
must really be true, the JWKS client is reused (off the event loop), and a
failed callback never echoes the IdP's response to the browser.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
import pytest

from server.auth import login, oauth, oidc, sso_links
from server.settings import settings
from tests.unit.oauth_store_fake import MemoryOAuthStore

pytestmark = pytest.mark.usefixtures("auth_attempts")

_ISS = "https://idp.example.com"


@pytest.fixture(autouse=True)
def _no_password_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No password account names the signed-in email unless a test says so."""

    async def none(email: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(sso_links, "unlinked_accounts", none)

    async def no_links(email: str) -> list[tuple[str, str]]:
        return []

    monkeypatch.setattr(sso_links, "reaching", no_links)


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "oidc_issuer", _ISS)
    monkeypatch.setattr(settings, "oauth_issuer", "https://loom.example")
    oidc._discovery_cache.clear()


@pytest.mark.parametrize(
    ("flag", "want"),
    [
        (True, "a@x.io"),
        ("true", "a@x.io"),
        ("TRUE", "a@x.io"),
        ("false", ""),
        (False, ""),
        (None, ""),
        (1, ""),
    ],
)
def test_email_counts_only_when_verified_is_really_true(flag: Any, want: str) -> None:
    assert oidc.verified_email({"email": " a@x.io ", "email_verified": flag}) == want


def _serve_discovery(monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any]) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=doc))
    real = httpx.AsyncClient

    monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


async def test_discovery_naming_another_issuer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_discovery(
        monkeypatch, {"issuer": "https://evil.example", "jwks_uri": "https://evil.example/k"}
    )
    with pytest.raises(ValueError, match="discovery issuer"):
        await oidc._discover()
    assert oidc._discovery_cache == {}


async def test_discovery_tolerates_a_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_discovery(monkeypatch, {"issuer": _ISS + "/", "jwks_uri": _ISS + "/k"})
    assert (await oidc._discover())["jwks_uri"] == _ISS + "/k"


def test_one_jwk_client_per_jwks_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str] = []

    class _Client:
        def __init__(self, uri: str, timeout: int, headers: dict[str, str]) -> None:
            built.append(uri)
            assert headers == {}  # no back-channel base set: the issuer's own address

        def get_signing_key_from_jwt(self, token: str) -> Any:
            return type("K", (), {"key": "k"})()

    monkeypatch.setattr(oidc, "PyJWKClient", _Client)
    oidc._jwk_clients.clear()
    for _ in range(3):
        assert oidc.signing_key("https://idp/jwks", "t") == "k"
    assert built == ["https://idp/jwks"]


_PENDING = {
    "kind": "mcp",
    "nonce": "n",
    "code_verifier": "v",
    "client_id": "c",
    "redirect_uri": "r",
    "code_challenge": "x",
    "mcp_state": "",
    "state": "s",
    "exp": 2**31 - 1,
}


class _Req:
    """A callback from the browser that started the sign-in (its cookie set)."""

    client = None  # no peer address

    def __init__(self) -> None:
        self.query_params = {"code": "c", "state": "s"}
        self.cookies = {oauth._state_cookie("s"): oauth.seal_pending(_PENDING)}


@pytest.mark.parametrize(
    ("exc", "status", "error"),
    [
        (httpx.ConnectError("idp down"), 502, "temporarily_unavailable"),
        (ValueError("secret-bearing IdP body"), 400, "oidc_error"),
    ],
)
async def test_a_failed_callback_does_not_echo_the_idp(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, status: int, error: str
) -> None:
    store = MemoryOAuthStore()
    monkeypatch.setattr(oauth, "store", store)

    async def boom(**kw: Any) -> Any:
        raise exc

    monkeypatch.setattr(oidc, "exchange_code", boom)
    page = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert page.status_code == status
    assert json.loads(page.body) == {"error": error}


async def test_an_unverified_email_is_refused_at_the_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryOAuthStore()
    monkeypatch.setattr(oauth, "store", store)

    async def exchange(**kw: Any) -> dict[str, str]:
        return {"id_token": "t"}

    async def verify(token: str, *, nonce: str) -> dict[str, Any]:
        return {"email": "a@x.io", "email_verified": "false"}

    denied: list[str] = []

    async def emit(event_type: str, **kw: Any) -> str:
        denied.append(event_type)
        return ""

    monkeypatch.setattr(oidc, "exchange_code", exchange)
    monkeypatch.setattr(oidc, "verify_id_token", verify)
    monkeypatch.setattr(oauth.audit, "emit", emit)
    page = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert page.status_code == 403
    assert b"verified email required" in page.body
    assert denied == ["auth.signin_denied"]


@pytest.mark.parametrize(
    ("status", "body", "refused"),
    [
        (400, {"error": "invalid_grant"}, True),  # the user or token is gone at the IdP
        (401, {"error": "invalid_client"}, False),  # Loom's secret is wrong: not the user's fault
        (400, {"error": "invalid_request"}, False),
        (503, {}, False),
    ],
)
async def test_only_invalid_grant_ends_a_session_at_refresh(
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any], refused: bool
) -> None:
    async def token_request(form: dict[str, str]) -> dict[str, Any]:
        response = httpx.Response(status, json=body, request=httpx.Request("POST", "https://idp"))
        raise httpx.HTTPStatusError("refused", request=response.request, response=response)

    monkeypatch.setattr(oidc, "_token_request", token_request)
    expected = oidc.UpstreamRefused if refused else oidc.UpstreamUnavailable
    with pytest.raises(expected):
        await oidc.refresh("idp-refresh")


@pytest.mark.parametrize("body", [{}, [], {"access_token": "a"}, {"token_type": "Bearer"}])
async def test_a_malformed_refresh_answer_is_the_idps_fault_not_a_success(
    monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    async def discover() -> dict[str, Any]:
        return {"token_endpoint": "https://idp.example/token"}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(oidc, "_discover", discover)
    monkeypatch.setattr(
        oidc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(respond), **kw)
    )
    with pytest.raises(oidc.UpstreamUnavailable):
        await oidc.refresh("idp-refresh")


async def test_a_callback_url_alone_does_not_finish_the_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round trip rides the starting browser's sealed cookie: the state in
    the callback URL lets nobody else make one."""
    store = MemoryOAuthStore()
    monkeypatch.setattr(oauth, "store", store)
    forged = _Req()
    forged.cookies = {oauth._state_cookie("s"): oauth.token_id("s")}  # all the URL gives
    page = await oauth.oidc_callback(forged)  # type: ignore[arg-type]
    assert page.status_code == 400
    assert store.keeper == {}  # nothing spent, nothing written


async def test_only_a_callback_the_idp_vouched_for_writes_and_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryOAuthStore()
    monkeypatch.setattr(oauth, "store", store)

    async def authorize_url(**kw: Any) -> str:
        return "https://idp/authorize"

    monkeypatch.setattr(oidc, "build_authorize_url", authorize_url)
    await oauth.start_oidc_login("dashboard", "203.0.113.5")
    assert store.keeper == {}  # an unauthenticated start costs the server nothing

    async def refused(**kw: Any) -> Any:
        raise ValueError("a made-up code")

    monkeypatch.setattr(oidc, "exchange_code", refused)
    assert (await oauth.oidc_callback(_Req())).status_code == 400  # type: ignore[arg-type]
    assert store.keeper == {}  # nor does a callback nobody signed in for

    async def exchanged(**kw: Any) -> dict[str, str]:
        return {"id_token": "t"}

    async def verified(token: str, *, nonce: str) -> dict[str, Any]:
        return {"email": "nobody@x.io", "email_verified": True}

    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    async def no_account(email: str) -> None:
        return None

    monkeypatch.setattr(oidc, "exchange_code", exchanged)
    monkeypatch.setattr(oidc, "verify_id_token", verified)
    monkeypatch.setattr(oauth.audit, "emit", emit)
    monkeypatch.setattr(login, "resolve_identity_for_email", no_account)
    assert (await oauth.oidc_callback(_Req())).status_code == 403  # type: ignore[arg-type]
    assert list(store.keeper) == ["callback:s"]
    replayed = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert replayed.status_code == 400


async def test_callbacks_the_idp_refuses_are_limited_per_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(**kw: Any) -> Any:
        raise ValueError("a made-up code")

    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oidc, "exchange_code", refused)
    monkeypatch.setattr(
        oauth, "_failed_callbacks_by_address", oauth.Attempts("oidc.failed_callback", 2, 900)
    )
    for _ in range(2):
        assert (await oauth.oidc_callback(_Req())).status_code == 400  # type: ignore[arg-type]
    assert (await oauth.oidc_callback(_Req())).status_code == 429  # type: ignore[arg-type]


async def test_sign_in_starts_are_limited_per_address_and_ipv6_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def authorize_url(**kw: Any) -> str:
        return "https://idp/authorize"

    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oidc, "build_authorize_url", authorize_url)
    monkeypatch.setattr(oauth, "_starts_by_address", oauth.Attempts("oidc.start", 2, 900))
    for host in ("2001:db8::1", "2001:db8::2"):
        assert (await oauth.start_oidc_login("dashboard", host)).status_code == 302
    assert (await oauth.start_oidc_login("dashboard", "2001:db8::3")).status_code == 429
    assert (await oauth.start_oidc_login("dashboard", "2001:db9::1")).status_code == 302


def test_an_ipv4_client_of_a_dual_stack_socket_counts_as_itself() -> None:
    from server.auth.attempts import address_key

    assert address_key("::ffff:203.0.113.5") == "203.0.113.5"
    assert address_key("::ffff:203.0.113.5") != address_key("::ffff:198.51.100.7")
    assert address_key("2001:db8::1") == address_key("2001:db8::2") == "2001:db8::/64"


@pytest.mark.parametrize(("idp_status", "status"), [(400, 400), (503, 502)])
async def test_a_code_the_idp_refuses_starts_again_and_its_outage_retries(
    monkeypatch: pytest.MonkeyPatch, idp_status: int, status: int
) -> None:
    request = httpx.Request("POST", "https://idp/token")

    async def answered(**kw: Any) -> Any:
        response = httpx.Response(idp_status, request=request, json={"error": "invalid_grant"})
        raise httpx.HTTPStatusError("refused", request=request, response=response)

    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oidc, "exchange_code", answered)
    page = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert page.status_code == status
    assert (b"has expired" in page.body) is (idp_status == 400)


async def test_error_callbacks_count_toward_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oauth.audit, "emit", emit)
    monkeypatch.setattr(
        oauth, "_failed_callbacks_by_address", oauth.Attempts("oidc.failed_callback", 2, 900)
    )
    cancelled = _Req()
    cancelled.query_params = {"error": "access_denied", "state": "s"}
    for _ in range(2):
        assert (await oauth.oidc_callback(cancelled)).status_code == 403  # type: ignore[arg-type]
    assert (await oauth.oidc_callback(cancelled)).status_code == 429  # type: ignore[arg-type]


async def test_a_rejected_client_secret_is_the_servers_fault_not_the_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://idp/token")

    async def rejected(**kw: Any) -> Any:
        response = httpx.Response(401, request=request, json={"error": "invalid_client"})
        raise httpx.HTTPStatusError("rejected", request=request, response=response)

    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oidc, "exchange_code", rejected)
    monkeypatch.setattr(
        oauth, "_failed_callbacks_by_address", oauth.Attempts("oidc.failed_callback", 1, 900)
    )
    for _ in range(3):  # never counted against the person's address
        assert (await oauth.oidc_callback(_Req())).status_code == 502  # type: ignore[arg-type]


async def test_only_a_callback_refused_before_the_idp_exchange_keeps_its_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import claims

    async def away(**kw: Any) -> Any:
        raise httpx.ConnectError("idp down")

    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(oidc, "exchange_code", away)
    page = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert page.status_code == 502 and "set-cookie" not in page.headers  # retry it

    async def exchanged(**kw: Any) -> dict[str, str]:
        return {"id_token": "t"}

    async def verified(token: str, *, nonce: str) -> dict[str, Any]:
        return {"email": "a@x.io", "email_verified": True}

    async def keeper_away(key: str, expires_at: int) -> bool:
        raise claims.Busy(key)

    monkeypatch.setattr(oidc, "exchange_code", exchanged)
    monkeypatch.setattr(oidc, "verify_id_token", verified)
    monkeypatch.setattr(oauth.store, "use_once", keeper_away)
    page = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    # The IdP code is spent: a reload could not finish it, so the page starts
    # a fresh sign-in where this one began.
    assert page.status_code == 503 and "Max-Age=0" in page.headers["set-cookie"]
    assert b'href="/oauth/authorize?' in page.body

    async def keys_away(token: str, *, nonce: str) -> dict[str, Any]:
        raise jwt.PyJWKClientConnectionError("jwks down")

    monkeypatch.setattr(oidc, "verify_id_token", keys_away)
    page = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    # Past the exchange too: the IdP's keys being away is no reason to reload.
    assert page.status_code == 503 and "Max-Age=0" in page.headers["set-cookie"]
    assert b'href="/oauth/authorize?' in page.body


def test_a_restart_begins_each_sign_in_where_it_began() -> None:
    from urllib.parse import parse_qs, urlsplit

    assert oauth._restart_url({"kind": "dashboard"}) == "/me?sign_in=sso"
    assert oauth._restart_url({"kind": "device", "user_code": "BCDFGHJK"}) == (
        "/oauth/device?user_code=BCDFGHJK"
    )
    mcp = urlsplit(oauth._restart_url(_PENDING | {"mcp_state": "st"}))
    assert mcp.path == "/oauth/authorize"
    assert parse_qs(mcp.query)["client_id"] == ["c"] and parse_qs(mcp.query)["state"] == ["st"]


@pytest.mark.parametrize("email", ["", "   "])
def test_no_email_resolves_to_nobody_whatever_rows_have_none(
    monkeypatch: pytest.MonkeyPatch, email: str
) -> None:
    # Agents' and bots' rows name no email: an empty one must never be looked
    # up, or it would land on one of them.
    import asyncio

    from server.identity.app import password_link

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            raise AssertionError("an empty email was looked up")

    monkeypatch.setattr(login, "get_client", lambda: _CH())
    monkeypatch.setattr(password_link, "get_client", lambda: _CH())
    monkeypatch.setattr(sso_links, "get_client", lambda: _CH())
    assert asyncio.run(login.resolve_identity_for_email(email)) is None
    assert asyncio.run(password_link.sso_accounts(email)) == []
    assert asyncio.run(sso_links.unlinked_accounts(email)) == []
    # Nor a blank sign-in identifier: unclaimed agents hold claim-code passwords.
    assert asyncio.run(login.resolve_user_credentials(email, "a-claim-code")) is None


class _StartReq:
    """The dashboard's `/oauth/sso`, with the tab's mark as `?n=` when given."""

    client = None

    def __init__(self, n: str | None) -> None:
        self.query_params = {} if n is None else {"n": n}


async def _sealed_dashboard_start(monkeypatch: pytest.MonkeyPatch, n: str | None) -> dict[str, Any]:
    """The sign-in record `/oauth/sso?n=` seals into its cookie."""
    from server.core.secrets_at_rest import open_sealed

    async def authorize_url(**kw: Any) -> str:
        return "https://idp/authorize"

    monkeypatch.setattr(oidc, "build_authorize_url", authorize_url)
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    response = await oauth.dashboard_sso(_StartReq(n))  # type: ignore[arg-type]
    [cookie] = [v for k, v in response.headers.items() if k == "set-cookie"]
    sealed = cookie.split("=", 1)[1].split(";", 1)[0].strip('"')
    record: dict[str, Any] = json.loads(open_sealed(oauth._PENDING_PURPOSE, sealed))
    return record


async def test_the_dashboard_tab_mark_is_sealed_with_its_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mark = "Ab_-" * 8
    assert (await _sealed_dashboard_start(monkeypatch, mark))["tab_nonce"] == mark


@pytest.mark.parametrize(
    "n", [None, "", "short", "has space in it padded", "x" * 65, "<script>x</script>xx"]
)
async def test_a_missing_or_malformed_tab_mark_is_not_sealed(
    monkeypatch: pytest.MonkeyPatch, n: str | None
) -> None:
    assert "tab_nonce" not in await _sealed_dashboard_start(monkeypatch, n)


async def _landing(monkeypatch: pytest.MonkeyPatch, pending: dict[str, Any]) -> str:
    from server.auth import revocation

    async def not_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return False

    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(revocation, "subjects_revoked", not_revoked)
    monkeypatch.setattr(oauth.audit, "emit", emit)
    response = await oauth._dashboard_signed_in(
        ("alice@x.io", "acme", ["team"], ["writer"]), pending
    )
    location: str = response.headers["location"]
    return location


async def test_the_dashboard_landing_hands_back_the_tab_mark_beside_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mark = "Ab_-" * 8
    location = await _landing(monkeypatch, {"kind": "dashboard", "tab_nonce": mark})
    # In the query, so a dashboard that predates the mark still reads the fragment as a token.
    assert location.startswith(f"/me?n={mark}#token=")
    assert "&" not in location.split("#", 1)[1]


@pytest.mark.parametrize(
    "pending", [{"kind": "dashboard"}, {"kind": "dashboard", "tab_nonce": "bad&n=x"}]
)
async def test_the_dashboard_landing_echoes_no_mark_it_was_not_given(
    monkeypatch: pytest.MonkeyPatch, pending: dict[str, Any]
) -> None:
    location = await _landing(monkeypatch, pending)
    assert location.startswith("/me#token=") and "&" not in location and "?" not in location
