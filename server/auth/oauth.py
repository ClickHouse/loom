"""OAuth 2.1 Authorization Server: sign-in for the CLI, the SDK and MCP clients.

`router` (mounted whenever auth is on) serves the device grant (device.py),
the token and revocation endpoints, the IdP callback and the dashboard's SSO
sign-in; `connect_router` adds MCP one-click connect when
`LOOM_OAUTH_ENABLED=true`. Loom issues its own JWTs; under
`LOOM_OAUTH_LOGIN_PROVIDER=oidc` the IdP only authenticates (oidc.py). State
lives in oauth_store.py. The flows, sessions and threat model are in
docs/architecture/security.md ("OIDC and OAuth").
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import secrets
import time
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from server.auth import agents, claims, machines, scim, shielded, sso_links
from server.auth.attempts import Attempts, Busy, address_key, admit, client_address, slot
from server.auth.jwt import AuthInfo, mint_token, new_jti
from server.auth.oauth_store import (
    PENDING_TTL,
    UPSTREAM_PURPOSE,
    ClickHouseStore,
    CodeReplayed,
    Device,
    Grant,
    OAuthStore,
    SessionRetired,
    token_id,
)
from server.core.errors import BackendUnavailableError, GrantBusyError
from server.core.secrets_at_rest import open_sealed, seal, unseal
from server.observability import audit
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.login import Identity

log = logging.getLogger(__name__)

router = APIRouter()
connect_router = APIRouter()


class OAuthError(Exception):
    """An OAuth error answer: `{"error": …, "error_description": …}` at the top
    of the body (RFC 6749 §5.2, RFC 8628 §3.5), which OAuth clients read to
    tell "keep polling" from "stop"."""

    def __init__(
        self,
        error: str,
        status_code: int = 400,
        *,
        description: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.status_code = status_code
        self.description = description
        self.headers = headers or {}


# On every token and device sign-in answer of a build that keeps sessions: a
# client that has seen it, told invalid_grant without it, heard a previous
# build mid-upgrade, which knows none, and keeps its session to ask again.
SESSIONS_HEADER = "Loom-Sessions"


def oauth_error_response(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, OAuthError)
    body = {"error": exc.error}
    if exc.description:
        body["error_description"] = exc.description
    headers = {"Cache-Control": "no-store", SESSIONS_HEADER: "1", **exc.headers}
    return JSONResponse(body, status_code=exc.status_code, headers=headers)


_CODE_TTL = 60  # auth code: short-lived, single-use
# 30d refresh token; each refresh issues a new one.
_REFRESH_TTL = 30 * 86400  # not a setting: a refresh reads the issue time back as exp minus this
store: OAuthStore = ClickHouseStore()


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------
def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def verify_pkce_s256(verifier: str, challenge: str) -> bool:
    """S256: challenge == base64url(sha256(verifier)), no padding."""
    if not verifier or not challenge:
        return False
    expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return secrets.compare_digest(expected, challenge)


def issuer(request: Request) -> str:
    """LOOM_OAUTH_ISSUER, else the address every other client-facing URL uses
    (LOOM_PUBLIC_BASE_URL, then the proxy's forwarded scheme and host)."""
    from server.auth.public_url import public_base_url

    return settings.oauth_issuer.rstrip("/") or public_base_url(request)


async def form_body(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body (the OAuth wire format)
    without pulling in python-multipart — last value wins per key. A body
    that is not UTF-8 is the client's error (400 invalid_request), not a 500."""
    try:
        raw = (await request.body()).decode("utf-8")
    except UnicodeDecodeError as e:
        raise OAuthError("invalid_request", description="body is not UTF-8") from e
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def authorization_server_metadata(issuer: str) -> dict[str, Any]:
    """Authorization-server metadata — what the client needs to drive the flow.
    The authorize and registration endpoints exist only with MCP connect on."""
    from server.auth.device import DEVICE_GRANT

    meta: dict[str, Any] = {
        "issuer": issuer,
        "token_endpoint": f"{issuer}/oauth/token",
        "device_authorization_endpoint": f"{issuer}/oauth/device_authorization",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "grant_types_supported": [
            DEVICE_GRANT,
            "refresh_token",
            "client_credentials",
            machines.TOKEN_EXCHANGE,
        ],
        "response_types_supported": [],  # required by RFC 8414; "code" with MCP connect
        "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        "token_endpoint_auth_signing_alg_values_supported": sorted(machines.KEY_ALGORITHMS),
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["loom"],
    }
    if settings.oauth_enabled:
        meta |= {
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "registration_endpoint": f"{issuer}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", *meta["grant_types_supported"]],
            "code_challenge_methods_supported": ["S256"],
        }
    return meta


# The sign-in pages run no script and may not be framed: a page that cannot be
# put under another site's overlay cannot be clickjacked into approving. Pinned
# here too, so the sign-in pages keep frame denial and their referrer policy
# whatever the middleware sends.
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    # same-origin, not no-referrer: under no-referrer a form POST carries `Origin: null`,
    # which web_refusal refuses. No other site receives this URL.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def html_page(
    title: str, body: str, status_code: int = 200, headers: dict[str, str] | None = None
) -> HTMLResponse:
    """One of the server-rendered sign-in pages. `body` is trusted HTML: every
    value in it must already be escaped."""
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui;max-width:26rem;margin:4rem auto;padding:0 1rem}}
input{{display:block;width:100%;padding:.6rem;margin:.4rem 0;box-sizing:border-box}}
button{{padding:.6rem 1rem;width:100%;margin:.3rem 0;background:#111;color:#fff;border:0;
border-radius:6px}} button.secondary{{background:#eee;color:#111}}
code{{font-size:1.4rem;letter-spacing:.15rem}} .error{{color:#b00020}}
</style></head><body>{body}</body></html>""",
        status_code=status_code,
        headers={**_PAGE_HEADERS, **(headers or {})},
    )


def protected_resource_metadata(issuer: str, resource: str) -> dict[str, Any]:
    """Protected-resource metadata — points the MCP client at this server."""
    return {
        "resource": resource,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
    }


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
@router.get("/.well-known/oauth-authorization-server")
async def as_metadata(request: Request) -> JSONResponse:
    return JSONResponse(authorization_server_metadata(issuer(request)))


@connect_router.get("/.well-known/oauth-protected-resource")
async def prm_metadata(request: Request) -> JSONResponse:
    iss = issuer(request)
    return JSONResponse(protected_resource_metadata(iss, f"{iss}/mcp"))


_MAX_REDIRECT_URIS = 10
_MAX_URI_CHARS = 2048
# Schemes a browser would execute or read locally instead of handing a code
# to a client.
_BAD_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "blob", "about"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def _destination(redirect_uri: str) -> str:
    """Where an authorization goes, as a consent page names it: the host, or
    the app scheme of a native client."""
    return urlsplit(redirect_uri).netloc or redirect_uri.split(":", 1)[0]


def _back_to_client(redirect_uri: str, params: dict[str, str], state: str) -> RedirectResponse:
    """Send the browser back to the MCP client with `params` (and its state)."""
    if state:
        params = {**params, "state": state}
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


def valid_redirect_uri(uri: str) -> bool:
    """OAuth 2.1 redirect URIs: absolute, no fragment; https, http only to a
    loopback host (native apps, RFC 8252 §7.3), or a private-use app scheme
    (`cursor://…`, RFC 8252 §7.1). The code goes to this URI, so a scheme the
    browser would run (`javascript:`) or a plain-http remote host is refused."""
    if not uri or len(uri) > _MAX_URI_CHARS or any(c.isspace() for c in uri):
        return False
    parts = urlsplit(uri)
    scheme = parts.scheme.lower()
    if not scheme or parts.fragment or "#" in uri or scheme in _BAD_SCHEMES:
        return False
    if scheme == "https":
        return bool(parts.hostname)
    if scheme == "http":
        return (parts.hostname or "") in _LOOPBACK
    return True


# --------------------------------------------------------------------------
# Dynamic client registration — public clients, no secret
# --------------------------------------------------------------------------
@connect_router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError as e:  # JSONDecodeError and UnicodeDecodeError both are
        raise OAuthError(
            "invalid_client_metadata", description="the registration body must be a JSON object"
        ) from e
    redirect_uris = body.get("redirect_uris") if isinstance(body, dict) else None
    if (
        not isinstance(redirect_uris, list)
        or not 0 < len(redirect_uris) <= _MAX_REDIRECT_URIS
        or not all(isinstance(u, str) and valid_redirect_uri(u) for u in redirect_uris)
    ):
        raise OAuthError(
            "invalid_redirect_uri",
            description="redirect_uris: 1-10 absolute URIs without a fragment; "
            "https, http to a loopback host, or an app scheme",
        )
    client_id = "loom-" + secrets.token_urlsafe(16)
    uris = list(redirect_uris)
    name = "".join(c for c in str(body.get("client_name") or "MCP client") if c.isprintable())
    await store.save_client(client_id, uris, name[:120] or "MCP client")
    return JSONResponse(
        {
            "client_id": client_id,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        status_code=201,
    )


# --------------------------------------------------------------------------
# Authorization endpoint
# --------------------------------------------------------------------------
async def _validate_authorize_params(
    client_id: str, redirect_uri: str, code_challenge: str, code_challenge_method: str
) -> dict[str, Any]:
    client: dict[str, Any] | None = await store.get_client(client_id)
    if client is None:
        raise OAuthError("invalid_client")
    if redirect_uri not in client["redirect_uris"]:
        raise OAuthError("invalid_redirect_uri")
    if code_challenge_method != "S256" or not code_challenge:
        # PKCE S256 is mandatory — no plaintext / no missing challenge.
        raise OAuthError("invalid_request", description="S256 PKCE required")
    return client


@connect_router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_form(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",
    response_type: str = "code",
    code_challenge: str = "",
    code_challenge_method: str = "",
    state: str = "",
    scope: str = "",
    resource: str = "",
) -> Response:
    if response_type != "code":
        raise OAuthError("unsupported_response_type")
    client = await _validate_authorize_params(
        client_id, redirect_uri, code_challenge, code_challenge_method
    )
    # Delegate the human sign-in to an external IdP when configured; loom still
    # mints the token after the callback maps the verified email to an account.
    if settings.oauth_login_provider == "oidc":
        return await start_oidc_login(
            "mcp",
            client_address(request),
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            mcp_state=state,
        )
    client_name = html.escape(client["client_name"])
    # Dynamic registration lets anyone name a client anything, so the page
    # also says where the authorization goes — the part a phisher cannot fake.
    destination = html.escape(_destination(redirect_uri))
    # Carry the OAuth params through the consent POST as hidden fields.
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "state": state,
            "scope": scope,
            "resource": resource,
        }.items()
    )
    return html_page(
        "Connect Loom",
        f"""<h2>Connect <b>{client_name}</b> to Loom</h2>
<p>Sign in to authorize this client to access your memories.
After you sign in, Loom sends the authorization to <b>{destination}</b>.</p>
<form method="post" action="/oauth/authorize">{hidden}
<input name="identifier" placeholder="email or namespace"
       autocomplete="username" required>
<input name="password" type="password" placeholder="password"
       autocomplete="current-password" required>
<button type="submit">Authorize</button>
</form>""",
    )


@connect_router.post("/oauth/authorize")
async def authorize_submit(request: Request) -> RedirectResponse:
    f = await form_body(request)
    client_id = f.get("client_id", "")
    redirect_uri = f.get("redirect_uri", "")
    code_challenge = f.get("code_challenge", "")
    code_challenge_method = f.get("code_challenge_method", "S256")
    state = f.get("state", "")
    await _validate_authorize_params(client_id, redirect_uri, code_challenge, code_challenge_method)
    if settings.oauth_login_provider == "oidc":
        # The GET sends people to the IdP; a password posted here would skip it.
        raise OAuthError("password_sign_in_disabled", 404)
    from server.auth.login import LoginThrottled, resolve_user_credentials

    # The code's issue time is taken before the revocations are read at its
    # exchange, so a revocation landing after that read postdates it. Its 60 s
    # lifetime counts from here, so the password check comes out of it.
    issued_at = int(time.time())
    try:
        resolved = await resolve_user_credentials(
            f.get("identifier", ""), f.get("password", ""), address=client_address(request)
        )
    except LoginThrottled as e:
        raise OAuthError("temporarily_unavailable", 429, headers={"Retry-After": "900"}) from e
    if resolved is None:
        # Bounce back to the form with an error (don't leak which field).
        raise OAuthError("access_denied", 401)
    return await _redirect_with_code(
        {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": code_challenge},
        resolved,
        upstream_refresh="",
        state=state,
        since=issued_at,
    )


async def _redirect_with_code(
    client: dict[str, str],
    identity: Identity,
    *,
    upstream_refresh: str,
    state: str,
    since: int,
) -> RedirectResponse:
    """Mint the MCP client's single-use code for a signed-in user and send the
    browser back to the client with it. `since` is when the identity was
    resolved: a subject revoked after it gets no tokens for the code."""
    subject, org, namespaces, roles = identity
    code = secrets.token_urlsafe(32)
    await store.save_code(
        code,
        {
            "client_id": client["client_id"],
            "redirect_uri": client["redirect_uri"],
            "code_challenge": client["code_challenge"],
            "subject": subject,
            "org": org,
            "namespaces": namespaces,
            "roles": roles,
            "exp": int(time.time()) + _CODE_TTL,
            "since": since,
            "family": new_jti(),
            "upstream_refresh": upstream_refresh,
        },
    )
    return _back_to_client(client["redirect_uri"], {"code": code}, state)


# --------------------------------------------------------------------------
# OIDC-brokered login: delegate the human sign-in to an external IdP, then map
# the verified email to a loom account. Three things start one: MCP connect
# (`mcp`), the device approval page (`device`) and the dashboard (`dashboard`);
# the callback finishes each its own way.
# --------------------------------------------------------------------------
def _oidc_callback_url() -> str:
    # From trusted config only (validate_config requires oauth_issuer under
    # oidc), never the request Host header — this URL is registered at the IdP
    # and a spoofed Host must not be able to redirect the code elsewhere.
    return f"{settings.oauth_issuer.rstrip('/')}/oauth/callback"


# The browser that starts an IdP sign-in carries a cookie named after its
# `state`, and the callback requires it: a sign-in started in one browser
# cannot be finished in another, so nobody can sign a victim into their own
# account by sending them a callback link. One cookie per sign-in, so two
# tabs signing in at once don't undo each other.
def _state_cookie(state: str) -> str:
    """The cookie carrying one IdP sign-in in flight."""
    return f"loom_oidc_{token_id(state)[:16]}"


_PENDING_PURPOSE = "oidc sign-in in flight"
_COOKIE_MAX = 3800  # browsers drop a cookie past 4 KB
_starts_by_address = Attempts("oidc.start", 300, 900)
# Callbacks that failed (cancelled, or a code the IdP would not exchange): a
# flood of them costs audit rows and calls to the IdP.
_failed_callbacks_by_address = Attempts("oidc.failed_callback", 30, 900)


def seal_pending(record: dict[str, Any]) -> str:
    return seal(_PENDING_PURPOSE, json.dumps(record))


def _pending_from(request: Request, state: str) -> dict[str, Any] | None:
    """The sign-in this browser started under `state`: sealed in its cookie,
    so a callback URL without the starting browser finishes nothing."""
    raw = open_sealed(_PENDING_PURPOSE, request.cookies.get(_state_cookie(state), ""))
    record: dict[str, Any] | None = json.loads(raw) if raw else None
    if record is None or record.get("state") != state or record["exp"] < int(time.time()):
        return None
    return record


async def start_oidc_login(kind: str, address: str, **data: str) -> Response:
    """Send the browser to the IdP; `data` comes back to the callback. The
    round trip rides the browser's own cookie, sealed, so starting a sign-in
    writes nothing on the server: only a callback the IdP sent back does."""
    from server.auth import oidc

    if not await _starts_by_address.allow(address_key(address)):
        log_refused("start", address)
        return html_page(
            "Sign in to Loom",
            "<h2>Too many sign-ins from here</h2><p>Try again in 15 minutes.</p>",
            429,
            headers={"Retry-After": "900"},
        )
    oidc_state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    record = {
        **data,
        "kind": kind,
        "nonce": nonce,
        "code_verifier": verifier,
        "state": oidc_state,
        "exp": int(time.time()) + PENDING_TTL,
    }
    cookie = seal_pending(record)
    if len(cookie) > _COOKIE_MAX:
        raise OAuthError("invalid_request", description="sign-in parameters too long")
    url = await oidc.build_authorize_url(
        redirect_uri=_oidc_callback_url(),
        state=oidc_state,
        nonce=nonce,
        code_challenge=_b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        offline=kind != "dashboard",  # the dashboard's token never refreshes
    )
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        _state_cookie(oidc_state),
        cookie,
        max_age=PENDING_TTL,
        path="/oauth/callback",
        secure=settings.oauth_issuer.startswith("https://"),
        httponly=True,
        samesite="lax",  # sent on the IdP's top-level redirect back
    )
    return response


_refused_logged_at: dict[str, float] = {}


def log_refused(what: str, address: str) -> None:
    _warn_once_a_minute(what, "oauth: too many sign-in %ss from %s; refused", what, address)


def _warn_once_a_minute(kind: str, message: str, *args: object) -> None:
    """At most once a minute per kind, so a flood shows up without filling
    the log."""
    if time.monotonic() - _refused_logged_at.get(kind, -60.0) > 60:
        _refused_logged_at[kind] = time.monotonic()
        log.warning(message, *args)


@router.get("/oauth/sso")
async def dashboard_sso(request: Request) -> Response:
    """The dashboard's "Sign in with SSO" button."""
    if settings.oauth_login_provider != "oidc":
        raise OAuthError("sso_not_configured", 404)
    return await start_oidc_login("dashboard", client_address(request))


async def _signin_denied(email: str, reason: str) -> None:
    await audit.emit(
        "auth.signin_denied",
        org="-",
        namespace="-",
        actor=email or "-",
        subject={"provider": "oidc", "reason": reason},
    )


def _denied_page(reason: str) -> Response:
    return html_page(
        "Sign-in denied",
        f"<h2>Sign-in denied</h2><p>{html.escape(reason)}.</p>",
        403,
    )


def _expired_sign_in() -> Response:
    return html_page(
        "Sign in to Loom",
        "<h2>This sign-in has expired</h2><p>Start it again from where you began.</p>",
        400,
    )


def _sign_in_unavailable(restart: str) -> Response:
    """Loom could not finish a sign-in it may not resume: the IdP's code is
    spent, so `restart` (where it began) starts a fresh one."""
    return html_page(
        "Sign in to Loom",
        "<h2>Sign-in is unavailable right now</h2>"
        f'<p><a href="{html.escape(restart)}">Start again</a> in a minute.</p>',
        503,
    )


def _restart_url(pending: dict[str, Any]) -> str:
    """Where the sign-in `pending` carries began, to begin it once more."""
    if pending["kind"] == "dashboard":
        return "/oauth/sso"
    if pending["kind"] == "device":
        return "/oauth/device?" + urlencode({"user_code": pending["user_code"]})
    return "/oauth/authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "code_challenge": pending["code_challenge"],
            "code_challenge_method": "S256",
            "state": pending["mcp_state"],
        }
    )


@router.get("/oauth/callback")
async def oidc_callback(request: Request) -> Response:
    try:
        response: Response = await _finish_oidc_login(request)
    except OAuthError as e:  # answered here, so its cookie goes too
        response = oauth_error_response(request, e)
    state = request.query_params.get("state", "")
    # Refused before the IdP exchange (it is away, or this address is over its
    # limit): the IdP code is unspent and a retry needs the round trip still.
    retryable = response.status_code in (429, 502)
    if state and not retryable:
        response.delete_cookie(_state_cookie(state), path="/oauth/callback")
    return response


class _StartAgain(Exception):
    """The IdP refused the code: a stale one, so the person starts again."""


class _Spent(Exception):
    """The IdP's code was exchanged but the sign-in could not finish."""


# States whose code is being exchanged in this process: an IdP code is spent
# once, so a second callback of the same sign-in waits for nothing.
_exchanging: set[str] = set()


async def _exchange_at_idp(
    request: Request, code: str, pending: dict[str, Any], address: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The IdP's tokens and verified id_token claims for `code`. A code it
    refuses raises _StartAgain and counts against `address`; a fault on its
    side (its outage, a rejected client secret) answers 502 and counts not.
    Its keys away once the code is spent raise _Spent: a reload cannot finish."""
    import jwt

    from server.auth import oidc

    try:
        if not await oidc.check_callback_issuer(request.query_params.get("iss", "")):
            raise OAuthError("invalid_issuer")
        tokens = await oidc.exchange_code(
            code=code,
            redirect_uri=_oidc_callback_url(),
            code_verifier=pending["code_verifier"],
        )
    except OAuthError:
        await _failed_callbacks_by_address.record(address)
        raise
    except httpx.HTTPStatusError as e:
        # Its response body stays in the log, not the browser.
        log.warning("oidc callback: the identity provider answered %s", e.response.status_code)
        if e.response.status_code == 400 and oidc.error_code(e.response) == "invalid_grant":
            await _failed_callbacks_by_address.record(address)
            raise _StartAgain from e  # a stale or made-up code
        raise OAuthError("temporarily_unavailable", 502) from e
    except httpx.TransportError as e:  # the IdP is away
        log.warning("oidc callback: identity provider exchange failed: %s", type(e).__name__)
        raise OAuthError("temporarily_unavailable", 502) from e
    except Exception as e:  # any other exchange failure refuses the login (re-raised)
        await _failed_callbacks_by_address.record(address)
        log.warning("oidc callback: identity provider exchange refused: %s", type(e).__name__)
        raise OAuthError("oidc_error") from e
    try:
        id_claims = await oidc.verify_id_token(tokens.get("id_token", ""), nonce=pending["nonce"])
    except jwt.PyJWKClientConnectionError as e:
        log.warning("oidc callback: the identity provider's keys are away: %s", e)
        await oidc.revoke(str(tokens.get("refresh_token") or ""))
        raise _Spent from e
    except Exception as e:  # any other verify failure refuses the login (re-raised)
        await _failed_callbacks_by_address.record(address)
        log.warning("oidc callback: id_token refused: %s", type(e).__name__)
        raise OAuthError("oidc_error") from e
    return tokens, id_claims


async def _finish_oidc_login(request: Request) -> Response:
    from server.auth import device, oidc

    state = request.query_params.get("state", "")
    if not state:
        return _expired_sign_in()
    pending = _pending_from(request, state)
    if pending is None:
        return _expired_sign_in()
    kind, user_code = pending["kind"], pending.get("user_code", "")
    address = address_key(client_address(request))
    if await _failed_callbacks_by_address.blocked(address):
        log_refused("callback", address)
        return html_page(
            "Sign in to Loom",
            "<h2>Too many failed sign-ins from here</h2><p>Try again in 15 minutes.</p>",
            429,
            headers={"Retry-After": "900"},
        )
    if request.query_params.get("error"):
        # Cancelled at the IdP, or refused there. A device grant stays pending:
        # who cancelled is unknown, so it is no reason to end anyone's sign-in.
        await _failed_callbacks_by_address.record(address)
        await _signin_denied("", "cancelled at the identity provider")
        return _denied_page("The identity provider did not sign you in; try again")
    code = request.query_params.get("code", "")
    if not code:
        raise OAuthError("invalid_request")
    if state in _exchanging:  # the same callback twice at once: an IdP code is spent once
        return _expired_sign_in()
    _exchanging.add(state)
    try:
        tokens, id_claims = await _exchange_at_idp(request, code, pending, address)
    except _StartAgain:
        return _expired_sign_in()
    except _Spent:
        return _sign_in_unavailable(_restart_url(pending))
    finally:
        _exchanging.discard(state)
    upstream = str(tokens.get("refresh_token") or "")
    # The IdP vouched for this person: only now is the round trip marked used,
    # so a callback nobody signed in for writes nothing.
    try:
        first = await store.use_once(f"callback:{state}", pending["exp"])
    except claims.Busy:  # Keeper cannot answer right now
        await oidc.revoke(upstream)
        return _sign_in_unavailable(_restart_url(pending))
    if not first:
        await oidc.revoke(upstream)
        return _expired_sign_in()
    email = oidc.verified_email(id_claims)
    resolved = await _account_for(email, pending, upstream) if email else None
    if isinstance(resolved, Response):
        return resolved
    if resolved is None or isinstance(resolved, _Denied):
        if isinstance(resolved, _Denied):
            reason = resolved.reason
        else:
            reason = "no loom account for this email" if email else "verified email required"
        await _signin_denied(str(id_claims.get("email") or ""), reason)
        await oidc.revoke(upstream)
        if kind == "device":
            await device.deny(user_code)
        return _denied_page(f"{reason}; ask your Loom operator for access")
    return await _signed_in_as(pending, resolved, upstream, email)


class _Denied(NamedTuple):
    """A verified email that signs in to no account, and why."""

    reason: str


async def _account_for(
    email: str, pending: dict[str, Any], upstream: str
) -> Identity | Response | _Denied | None:
    """The account a verified `email` signs in to, a page to answer instead
    (link a password account, or try again later), why it signs in to none,
    or None: no account."""
    from server.auth import oidc
    from server.auth.jit import finish_unfinished_account, jit_provision_for_email
    from server.auth.login import SignInUnavailable, resolve_identity_for_email

    try:
        if await scim.deactivated(email):  # before JIT makes them an account
            return _Denied(_DEACTIVATED)
        await finish_unfinished_account(email)
        resolved = await resolve_identity_for_email(email)
        if resolved is not None:
            return resolved
        if await sso_links.reaching(email):
            # Linked accounts that disagree (owners, orgs): fail closed, saying so.
            return _Denied("this email's linked accounts do not resolve to one account")
        if await sso_links.unlinked_accounts(email):
            # Their password account: proving it links it, no new one is made.
            return await _offer_link(pending, email, upstream)
        # No account — auto-provision one if JIT is on (else stay fail-closed).
        return await jit_provision_for_email(email)
    except SignInUnavailable as e:
        log.warning("oidc callback: the account could not be read: %s", e)
        await oidc.revoke(upstream)
        return _sign_in_unavailable(_restart_url(pending))


_DEACTIVATED = "this account is deactivated"


async def _signed_in_as(
    pending: dict[str, Any],
    identity: Identity,
    upstream: str,
    email: str,
) -> Response:
    """Carry on the sign-in `pending` began, as `identity`. Every SSO sign-in
    ends here, so here is where a SCIM deactivation is sure to be checked."""
    from server.auth import device

    if refused := await _refused_if_deactivated(email, pending, upstream):
        return refused
    if pending["kind"] == "device":
        return await device.signed_in(pending["user_code"], identity, upstream)
    if pending["kind"] == "dashboard":
        return await _dashboard_signed_in(identity, pending)
    return await _connect_consent(pending, identity, upstream)


async def _refused_if_deactivated(
    email: str, pending: dict[str, Any], upstream: str
) -> Response | None:
    """The answer to a sign-in by `email` that SCIM has deactivated (or that
    cannot be checked), or None to carry on."""
    from server.auth import device, oidc
    from server.auth.login import SignInUnavailable

    try:
        deactivated = await scim.deactivated(email)
    except SignInUnavailable:
        await oidc.revoke(upstream)
        return _sign_in_unavailable(_restart_url(pending))
    if not deactivated:
        return None
    await _signin_denied(email, _DEACTIVATED)
    await oidc.revoke(upstream)
    if pending["kind"] == "device":
        await device.deny(pending["user_code"])
    return _denied_page(f"{_DEACTIVATED}; ask your Loom operator for access")


def _link_cookie(ticket: str) -> str:
    """The cookie binding a link ticket to the browser that did the IdP round
    trip, one per ticket so two sign-ins in one browser keep theirs: a ticket
    seen elsewhere (a log, a shared screen) finishes nothing on its own."""
    return f"loom_link_{token_id(ticket)[:12]}"


async def _offer_link(pending: dict[str, Any], email: str, upstream: str) -> Response:
    ticket, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    try:
        await store.save_pending(
            token_id(ticket),
            {
                "kind": "link",
                "email": email,
                "binding": token_id(binding),
                "sign_in": pending,
                "upstream_refresh": seal(UPSTREAM_PURPOSE, upstream),
            },
        )
    except claims.Busy:
        from server.auth import oidc

        await oidc.revoke(upstream)
        return _sign_in_unavailable(_restart_url(pending))
    response = _link_page(ticket, email)
    response.set_cookie(
        _link_cookie(ticket),
        binding,
        max_age=PENDING_TTL,
        path="/oauth/link",
        secure=settings.oauth_issuer.startswith("https://"),
        httponly=True,
        samesite="strict",  # posted only from the link page itself
    )
    return response


def _link_page(ticket: str, email: str, error: str = "", status_code: int = 200) -> Response:
    message = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return html_page(
        "Sign in to Loom",
        f"""<h2>Link your Loom account</h2>
<p>A Loom account with a password already uses <b>{html.escape(email)}</b>.
Enter that password once, and single sign-on opens this account from now on,
with all of its namespaces and data.</p>{message}
<form method="post" action="/oauth/link">
<input type="hidden" name="ticket" value="{ticket}">
<input name="password" type="password" placeholder="Loom password"
       autocomplete="current-password" required>
<button type="submit">Link and continue</button>
<button type="submit" name="decision" value="not_mine" class="secondary" formnovalidate>
That isn't my account</button>
</form>
<p>No longer know it? Your Loom operator can link it for you.</p>""",
        status_code,
    )


@router.post("/oauth/link")
async def link_account(request: Request) -> Response:
    """The person the IdP vouched for proves the password of the account that
    names their email; that account is then theirs under SSO."""
    from server.auth import oidc
    from server.auth.login import LoginThrottled, resolve_user_credentials

    f = await form_body(request)
    ticket = f.get("ticket", "")
    held = await store.pending(token_id(ticket))
    binding = token_id(request.cookies.get(_link_cookie(ticket), ""))
    if (
        held is None
        or held.get("kind") != "link"
        or not secrets.compare_digest(binding, held.get("binding", ""))
    ):
        return _expired_sign_in()
    email, sign_in = held["email"], held["sign_in"]
    proven = None
    if f.get("decision") != "not_mine":
        try:
            proven = await resolve_user_credentials(
                email, f.get("password", ""), address=client_address(request)
            )
        except LoginThrottled:
            return _link_page(ticket, email, "Too many failed attempts. Try again later.", 429)
        if proven is None:
            return _link_page(ticket, email, "That is not the account's password.", 401)
    if await store.take_pending(token_id(ticket)) is None:
        return _expired_sign_in()  # the form sent twice: the other one carries on
    upstream = unseal(UPSTREAM_PURPOSE, held["upstream_refresh"])
    # Before any link or account is written: the page may have been open since before
    # a deactivation, and what it wrote would be reachable but never revoked.
    if refused := await _refused_if_deactivated(email, sign_in, upstream):
        refused.delete_cookie(_link_cookie(ticket), path="/oauth/link")
        return refused
    try:
        if proven is None:
            response = await _declined_password_account(email, sign_in, upstream)
        else:
            response = await _link_proven(email, proven, f.get("password", ""), sign_in, upstream)
    except Exception as e:  # noqa: BLE001 — the ticket is spent: the person starts again
        log.warning("oauth: linking %s's account failed: %r", email, e)
        await oidc.revoke(upstream)
        response = _sign_in_unavailable(_restart_url(sign_in))
    response.delete_cookie(_link_cookie(ticket), path="/oauth/link")
    return response


async def _link_proven(
    email: str, proven: Identity, password: str, sign_in: dict[str, Any], upstream: str
) -> Response:
    from server.auth import oidc
    from server.auth.login import resolve_identity_for_email
    from server.auth.passwords import verify_password_off_loop
    from server.core.ch import own_write_reads

    subject, org, namespaces, _ = proven
    # The password proves the accounts behind its own hash, not every one the
    # identity reaches (a restricted namespace granted to it) or names the email.
    accounts = [
        (a["org"], a["namespace"])
        for a in await sso_links.unlinked_accounts(email)
        if a["org"] == org
        and a["namespace"] in namespaces
        and await verify_password_off_loop(a["password_hash"], password)
    ]
    accounts = await sso_links.link_live(email, accounts, "self")
    await audit.emit(
        "auth.sso_linked",
        org=org,
        namespace="-",
        actor=subject,
        subject={"email": email, "by": "self", "namespaces": [ns for _, ns in accounts]},
    )
    with own_write_reads():
        resolved = await resolve_identity_for_email(email)
    if resolved is None:  # its accounts disagree (orgs, owners): the guard holds
        await _signin_denied(email, "linked account does not resolve")
        await oidc.revoke(upstream)
        return _denied_page("this email names several accounts; ask your Loom operator")
    return await _signed_in_as(sign_in, resolved, upstream, email)


async def _declined_password_account(
    email: str, sign_in: dict[str, Any], upstream: str
) -> Response:
    """Someone else registered the address with a password. In the org JIT
    provisions into, no second account is made beside it: a password sign-in
    with the address would still find that account, and a revocation of the
    address would reach both (`aliases.spellings`), so the person is sent to
    the operator, who frees
    the address by releasing that account. One in another org shares nothing
    with the JIT org, and the person goes on to an account of their own."""
    from server.auth import device, oidc
    from server.auth.jit import jit_provision_for_email
    from server.auth.login import SignInUnavailable

    declined = await sso_links.unlinked_accounts(email)
    # The signal an operator looks for when an address was squatted.
    await audit.emit(
        "auth.sso_link_declined",
        org="-",
        namespace="-",
        actor=email,
        subject={"namespaces": [a["namespace"] for a in declined]},
    )
    if any(a["org"] == settings.oidc_jit_org for a in declined):
        reason, advice = "another account uses this email", "ask your Loom operator to release it"
        identity = None
    else:
        reason, advice = "no loom account for this email", "ask your Loom operator for access"
        try:
            identity = await jit_provision_for_email(email)
        except SignInUnavailable:
            await oidc.revoke(upstream)
            return _sign_in_unavailable(_restart_url(sign_in))
    if identity is None:
        await _signin_denied(email, reason)
        await oidc.revoke(upstream)
        if sign_in["kind"] == "device":
            await device.deny(sign_in["user_code"])
        return _denied_page(f"{reason}; {advice}")
    return await _signed_in_as(sign_in, identity, upstream, email)


async def _connect_consent(
    pending: dict[str, Any],
    identity: Identity,
    upstream_refresh: str,
) -> Response:
    """Under OIDC the IdP may sign the user in without a single click, so the
    MCP client gets its code only after the user approves here, on a page that
    names the client and where the authorization goes."""
    client = await store.get_client(pending["client_id"])
    if client is None:
        raise OAuthError("invalid_client")
    ticket = secrets.token_urlsafe(32)
    try:
        await store.save_pending(
            token_id(ticket),
            {
                "kind": "connect",
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "code_challenge": pending["code_challenge"],
                "mcp_state": pending["mcp_state"],
                "identity": list(identity),
                "since": int(time.time()),  # the identity was resolved now
                "upstream_refresh": seal(UPSTREAM_PURPOSE, upstream_refresh),
            },
        )
    except claims.Busy:
        from server.auth import oidc

        await oidc.revoke(upstream_refresh)
        return _sign_in_unavailable(_restart_url(pending))
    subject, org = identity[0], identity[1]
    destination = _destination(pending["redirect_uri"])
    return html_page(
        "Connect Loom",
        f"""<h2>Connect <b>{html.escape(client["client_name"])}</b> to Loom?</h2>
<p>Signed in as <b>{html.escape(subject)}</b> ({html.escape(org)}).</p>
<p>Approving lets this client read and write your memories. Loom sends the
authorization to <b>{html.escape(destination)}</b>.</p>
<form method="post" action="/oauth/authorize/approve">
<input type="hidden" name="ticket" value="{ticket}">
<button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" class="secondary">Deny</button>
</form>""",
    )


@connect_router.post("/oauth/authorize/approve")
async def connect_approve(request: Request) -> Response:
    f = await form_body(request)
    try:
        return await _decide_connect(f)
    except claims.Busy:  # nothing was spent: the same choice can be sent again
        return html_page(
            "Connect Loom",
            "<h2>Loom is busy</h2><p>Go back and submit your choice again in a moment.</p>",
            503,
        )


async def _decide_connect(f: dict[str, str]) -> Response:
    ticket = token_id(f.get("ticket", ""))
    expired = html_page("Connect Loom", "<h2>This approval has expired</h2>", 400)
    pending = await store.pending(ticket)
    if pending is None or pending.get("kind") != "connect":
        return expired
    subject, org, namespaces, roles = pending["identity"]
    approved = f.get("decision") == "approve"
    client = {
        "client_id": pending["client_id"],
        "redirect_uri": pending["redirect_uri"],
        "code_challenge": pending["code_challenge"],
    }
    # Minted before the ticket is spent, so a failed write leaves the approval
    # to submit again; of two submits only the one that spends the ticket answers.
    answer = None
    if approved:
        answer = await _redirect_with_code(
            client,
            (subject, org, namespaces, roles),
            upstream_refresh=unseal(UPSTREAM_PURPOSE, pending["upstream_refresh"]),
            state=pending["mcp_state"],
            since=pending["since"],
        )
    if await store.take_pending(ticket) is None:
        return expired  # the other submit answered
    await audit.emit(
        "auth.connect_approved" if approved else "auth.connect_denied",
        org=org,
        namespace="-",
        actor=subject,
        subject={"client_id": pending["client_id"], "redirect_uri": pending["redirect_uri"]},
    )
    if answer is not None:
        return answer
    from server.auth import oidc

    await oidc.revoke(unseal(UPSTREAM_PURPOSE, pending["upstream_refresh"]))
    return _back_to_client(client["redirect_uri"], {"error": "access_denied"}, pending["mcp_state"])


async def _dashboard_signed_in(identity: Identity, pending: dict[str, Any]) -> Response:
    """Deliver a short-lived token at `/me`, which never redirects to another origin."""
    from server.auth.revocation import subjects_revoked

    subject, org, namespaces, roles = identity
    issued_at = int(time.time())  # before the read: a revocation after it postdates the token
    try:
        revoked = await subjects_revoked([subject], issued_at=issued_at)
    except Exception as e:  # noqa: BLE001 — unchecked, nobody is signed in: the user retries
        log.warning("dashboard sign-in: revocations not read: %s", e)
        return _sign_in_unavailable(_restart_url(pending))
    if revoked:
        await _signin_denied(subject, "revoked")
        return _denied_page("this account's tokens are revoked")
    family = new_jti()
    token = mint_token(
        subject=subject,
        org=org,
        namespaces=namespaces,
        roles=roles,
        ttl_seconds=settings.oauth_access_token_ttl_seconds,
        is_service_account=False,
        # A sign-in session like the CLI's: revocable as one, and what it mints
        # is bounded like the person, not by its access-token lifetime.
        session_id=family,
        first_party=True,
        issued_at=issued_at,
    )
    await audit.emit(
        "auth.sso_signin",
        org=org,
        namespace="-",
        actor=subject,
        subject={"client": "dashboard", "family": family, "namespaces": namespaces, "roles": roles},
    )
    return RedirectResponse(url=f"/me#token={token}", status_code=302)


# --------------------------------------------------------------------------
# Token endpoint
# --------------------------------------------------------------------------
def _invalid_grant(description: str = "") -> OAuthError:
    return OAuthError("invalid_grant", description=description)


async def issue_tokens(
    grant: Grant | Device, client_id: str, since: int, generation: int = 0
) -> dict[str, Any]:
    """An access token for the grant's session, and the next refresh token.
    Under OIDC a session with no IdP refresh token gets no Loom refresh token:
    nothing could tell Loom the user was deactivated, so the session ends with
    its access token. A subject revoked since `since` (when the grant was
    issued) gets nothing; that is read from ClickHouse, not this process's
    snapshot, and a failed read raises so the caller puts the credential back.
    `generation` is the refresh token's place in its session's rotations."""
    from server.auth.device import DEVICE_CLIENT_ID
    from server.auth.revocation import subjects_revoked

    issued_at = int(time.time())  # before the read: a revocation after it postdates the tokens
    if await subjects_revoked([grant["subject"]], issued_at=since):
        raise _invalid_grant("this account's tokens are revoked")
    access = mint_token(
        subject=grant["subject"],
        org=grant["org"],
        namespaces=list(grant["namespaces"]),
        roles=list(grant["roles"]),
        ttl_seconds=settings.oauth_access_token_ttl_seconds,
        is_service_account=False,
        session_id=grant["family"] or None,
        # A third-party MCP client's session mints nothing that outlives it.
        first_party=client_id == DEVICE_CLIENT_ID,
        issued_at=issued_at,
    )
    body = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": settings.oauth_access_token_ttl_seconds,
        "scope": "loom",
    }
    if settings.oauth_login_provider == "oidc" and not grant["upstream_refresh"]:
        return body
    refresh = secrets.token_urlsafe(32)
    await store.save_refresh(
        refresh,
        {
            "client_id": client_id,
            "redirect_uri": "",
            "code_challenge": "",
            "subject": grant["subject"],
            "org": grant["org"],
            "namespaces": list(grant["namespaces"]),
            "roles": list(grant["roles"]),
            "exp": issued_at + _REFRESH_TTL,
            "family": grant["family"],
            "generation": generation,
            "upstream_refresh": grant["upstream_refresh"],
            "held": grant.get("held", {}),  # a rotation swaps from the token it spent
        },
    )
    body["refresh_token"] = refresh
    return body


async def end_session(grant: Grant | Device, reason: str) -> None:
    """Revoke the grant's session family: its refresh tokens stop refreshing and
    its access tokens stop verifying. Tokens minted from a `loom login` or
    dashboard SSO session are their owner's to revoke: an automatic verdict
    that could be wrong (a lost answer looks like a replay) never reaches them.
    Those a third-party MCP client's session minted name the session in their
    `parents` (`delegation.delegate`) and end with it.

    When the revocation cannot be written, a `revocation_owed` row stands in
    for it: the revocation snapshot every process loads counts the family as
    revoked. Raises only when neither landed."""
    from server.auth.revocation import revoke, revoked_here

    family = grant["family"]
    if not family:
        return  # an empty family names no session to revoke
    until = datetime.now(UTC) + timedelta(seconds=_REFRESH_TTL)
    try:
        await revoke(family, expires_at=until, reason=reason, revoked_by="oauth")
    except Exception:  # the owed row stands in for it, or the error goes on
        log.exception("oauth: revoking session %s failed; recording it as owed", family)
        await store.owe_revocation(family, reason)
        revoked_here(family)  # other processes read the owed row at their next refresh
    await _audit_ended(grant, reason)


async def _audit_ended(grant: Grant | Device, reason: str) -> None:
    await audit.emit(
        "auth.session_revoked",
        org=grant["org"],
        namespace="-",
        actor=grant["subject"],
        subject={"family": grant["family"], "client_id": grant["client_id"], "reason": reason},
    )


async def put_back(saving: Coroutine[Any, Any, None]) -> None:
    """Return a spent credential from a request that failed or is being
    cancelled: held to its end, within _PUT_BACK_S, even if a cancellation
    comes (shutdown drains it before ClickHouse goes), and a failure logged
    from inside it, so it neither hides the request's own outcome nor goes
    unseen."""

    async def logged() -> None:
        try:
            async with asyncio.timeout(_PUT_BACK_S):
                await saving
        except Exception as e:  # noqa: BLE001 — the request's own outcome is what goes on
            log.warning("oauth: could not put a credential back: %r", e)

    await shielded.shielded(logged(), name="oauth put-back")


def _unavailable() -> OAuthError:
    return OAuthError("temporarily_unavailable", 503, headers={"Retry-After": "5"})


_REPLAYED = (
    "this session was ended because a spent {what} was presented again: "
    "a copy of it is in other hands, or an answer was lost"
)


async def end_replayed_session(e: CodeReplayed, what: str) -> OAuthError:
    """A spent credential came back: a copy is in other hands, or a client lost
    an answer. Either way the session ends; its owner signs in again, told why,
    so a theft is not taken for an expiry. While the end is not recorded
    anywhere, the answer is "retry", not a final refusal."""
    family = e.grant["family"]
    ended = bool(family) and await _ended(family)
    if ended:  # a logout spent it, or an earlier replay ended the session
        log.warning("oauth: a spent %s of ended session %s was presented again", what, family)
        return _invalid_grant("this session has ended; sign in again")
    try:
        await end_session(e.grant, f"{what} replayed")
    except Exception:  # nothing durable landed: the presenter is told to retry
        log.exception("oauth: ending a replayed %s's session failed", what)
        return _unavailable()
    await _revoke_upstream(e.grant)  # may be rotated already
    return _invalid_grant(_REPLAYED.format(what=what))


async def _authorization_code_grant(f: dict[str, str]) -> dict[str, Any]:
    client_id = f.get("client_id", "")
    code, verifier = f.get("code", ""), f.get("code_verifier", "")

    def presented_right(g: Grant) -> bool:
        return g["redirect_uri"] == f.get("redirect_uri", "") and verify_pkce_s256(
            verifier, g["code_challenge"]
        )

    try:
        grant = await store.take_code(code, client_id, presented_right)  # single-use
    except CodeReplayed as e:
        # Only the client itself (its verifier) presents it again: a leaked
        # code alone was refused above, unspent.
        raise await end_replayed_session(e, "authorization code") from None
    if grant is None:
        raise _invalid_grant()
    try:
        return await issue_tokens(grant, client_id, since=grant["since"])
    except OAuthError:
        raise
    except asyncio.CancelledError:  # cancelled at shutdown: the code is the client's again
        await put_back(store.save_code(code, grant))
        raise
    except Exception as e:  # Loom's own failure: unspend the code so the retry is no replay
        log.warning("oauth: issuing tokens for a code failed: %s", e)
        await put_back(store.save_code(code, grant))
        raise _unavailable() from e


async def _ask_identity_provider(grant: Grant, refresh_token: str) -> str:
    """Spend the session's IdP refresh token; the IdP refusing means the user
    is gone there (deactivated, signed out everywhere), so the session ends.
    Returns the IdP refresh token to keep."""
    from server.auth import oidc

    if not grant["upstream_refresh"]:
        raise _invalid_grant("sign in again")  # signed in before SSO was on
    try:
        return await oidc.refresh(grant["upstream_refresh"])
    except oidc.UpstreamRefused:
        # Family only: the IdP also says this when its refresh token merely
        # expired, which is no reason to end the person's machine tokens.
        if grant.get("taken_over"):  # likely the token the dead process had rotated
            await end_session(grant, "identity provider refused a taken-over refresh")
            unfinished = "this sign-in's last refresh did not finish; sign in again"
            raise _invalid_grant(unfinished) from None
        await end_session(grant, "identity provider refused the user")
        raise _invalid_grant("the identity provider ended this session") from None
    except oidc.UpstreamUnavailable as e:
        # Not the user's fault: put the spent token back so the retry works.
        log.warning("oauth refresh: identity provider unavailable: %s", e)
        await put_back(store.save_refresh(refresh_token, grant))
        raise OAuthError("temporarily_unavailable", 503, headers={"Retry-After": "5"}) from e


async def _ended(family: str) -> bool:
    """Whether the session has ended, from storage; unknown counts as not."""
    try:
        return await store.session_ended(family)
    except Exception:  # unreadable: ending it again only replaces its stored reason
        log.exception("oauth: could not read whether session %s has ended", family)
        return False


async def _revoke_upstream(grant: Grant | Device) -> None:
    """Best effort: revoke the session's IdP refresh token once Loom's ends."""
    if grant["upstream_refresh"] and settings.oauth_login_provider == "oidc":
        from server.auth import oidc

        await oidc.revoke(grant["upstream_refresh"])


# A refresh runs, its spend included, for at most _REFRESH_DEADLINE_S, and a
# failed one's put-back for _PUT_BACK_S more; each can end in a cancelled
# write's undo (claims.UNDO_S). At most 45 s: inside the SDK's 60-second wait,
# and well before another request may call the spend abandoned (_ABANDONED_S).
_REFRESH_DEADLINE_S = 25.0
_PUT_BACK_S = 10.0


async def _refresh_token_grant(f: dict[str, str]) -> dict[str, Any]:
    # A public client identifies itself on refresh (RFC 6749 §6), and the
    # token refreshes only for the client it was issued to; a mismatch is
    # refused unconsumed.
    client_id = f.get("client_id", "")
    if not client_id:
        raise OAuthError("invalid_request", description="client_id required")
    refresh_token = f.get("refresh_token", "")
    deadline = asyncio.get_running_loop().time() + _REFRESH_DEADLINE_S
    try:
        async with asyncio.timeout_at(deadline):
            grant = await store.take_refresh(refresh_token, client_id)  # rotates
    except CodeReplayed as e:
        raise await end_replayed_session(e, "refresh token") from None
    except SessionRetired:
        raise _invalid_grant("this session has ended; sign in again") from None
    except TimeoutError:  # the spend, if it landed, was undone (claims.swap)
        raise _unavailable() from None
    if grant is None:
        raise _invalid_grant()
    try:
        async with asyncio.timeout_at(deadline):
            return await _refresh_session(grant, refresh_token)
    except OAuthError as e:
        if e.status_code != 503:  # refused for good: no token replaces this one
            await store.retire_refresh(grant)
            await _revoke_upstream(grant)  # the IdP's, rotated before the refusal or not
        raise  # an answer: refused, or retry later with the token put back
    except asyncio.CancelledError:  # cancelled at shutdown: the token is the client's again
        await put_back(store.save_refresh(refresh_token, grant))
        raise
    except Exception as e:  # Loom's own failure (or too slow): unspend the token for the retry
        log.warning("oauth: refreshing a session failed: %r", e)
        await put_back(store.save_refresh(refresh_token, grant))  # with the IdP token it rotated
        raise _unavailable() from e


async def _refresh_session(grant: Grant, refresh_token: str) -> dict[str, Any]:
    """The refresh grant's checks and new tokens, once the old token is spent."""
    from server.auth.login import refreshed_roles

    # exp is always saved as mint time + _REFRESH_TTL.
    issued_at = int(grant["exp"]) - _REFRESH_TTL
    # Read from storage, not this process's snapshot: a session ended a moment
    # ago on another process (or owed its revocation) mints nothing more.
    if await store.session_ended(grant["family"]):
        raise _invalid_grant()
    if settings.oauth_login_provider == "oidc":
        grant["upstream_refresh"] = await _ask_identity_provider(grant, refresh_token)
    elif grant["upstream_refresh"]:
        # Signed in through SSO, now off: nothing vouches for the user any more.
        raise _invalid_grant("single sign-on is off here; sign in again")
    # The grant is a sign-in snapshot; each refresh re-checks the account
    # is still active and re-derives its roles, so neither a released
    # account nor a removed host rides a rolling 30-day chain.
    roles = await refreshed_roles(grant["subject"], grant["org"], list(grant["roles"]))
    if roles is None:
        await end_session(grant, "account no longer active")
        raise _invalid_grant()
    return await issue_tokens(
        {**grant, "roles": roles},
        grant["client_id"],
        since=issued_at,
        generation=grant.get("generation", 0) + 1,
    )


def _refused(e: machines.Refused) -> OAuthError:
    status = {"invalid_client": 401, "temporarily_unavailable": 503}.get(e.error, 400)
    return OAuthError(e.error, status, description=e.description)


async def _client_credentials_grant(f: dict[str, str], audiences: list[str]) -> dict[str, Any]:
    """Exchange a bot's key proof for a token within its lifetime limit."""
    assertion = f.get("client_assertion", "")
    if f.get("client_assertion_type") != machines.ASSERTION_TYPE or not assertion:
        raise OAuthError(
            "invalid_client", 401, description="send a private_key_jwt client assertion"
        )
    since = int(time.time())  # before the bot is read: see _machine_tokens
    try:
        asked = machines.asked_lifetime(f.get("expires_in"))  # before its assertion is spent
        m, proof = await machines.verify_assertion(assertion, f.get("client_id", ""), audiences)
    except machines.Refused as e:
        raise _refused(e) from None
    if not await store.use_once(
        f"assertion:{token_id(m['name'] + ':' + str(proof['jti']))}", int(proof["exp"]) + 60
    ):
        raise OAuthError(
            "invalid_client", 401, description="this client assertion was used already"
        )
    ttl = machines.token_lifetime(m, asked)
    return await _machine_tokens(m, since, since + ttl, {"by": "key"})


async def _token_exchange_grant(f: dict[str, str]) -> dict[str, Any]:
    """Exchange a workload token without outliving its proof or the bot's limit."""
    token = f.get("subject_token", "")
    if f.get("subject_token_type") not in machines.SUBJECT_TOKEN_TYPES or not token:
        raise OAuthError("invalid_request", description="send the workload's OIDC token as a jwt")
    since = int(time.time())  # before the bot is read: see _machine_tokens
    agent = own = ""
    try:
        asked = machines.asked_lifetime(f.get("expires_in"))  # before anything is counted
        client_id = f.get("client_id", "")
        if client_id:
            m, proof = await machines.verify_workload(token, client_id)
        else:
            m, proof = await machines.verify_any_workload(token)
        if m["agent_claim"]:
            agent = agents.agent_of(m, proof)
        await _admit_exchange(m["name"], agent)
        if agent:
            own = await agents.ensure_namespace(m, agent)
    except machines.Refused as e:
        raise _refused(e) from None
    except (GrantBusyError, BackendUnavailableError):  # its first exchange, made elsewhere now
        raise _unavailable() from None
    # The token never outlives the proof, nor its lifetime from `since`: its
    # issue time, which revocation is judged by.
    ends = min(int(proof["exp"]), since + machines.token_lifetime(m, asked))
    issued = await _machine_tokens(
        m, since, ends, {"by": "workload", "workload_sub": str(proof["sub"])}, agent, own
    )
    return {**issued, "issued_token_type": "urn:ietf:params:oauth:token-type:access_token"}


# Seconds both exchange limits count over (LOOM_WORKLOAD_EXCHANGES_PER_*).
_EXCHANGE_WINDOW_S = 600  # not a setting: the window both limits are stated over


async def _admit_exchange(trust: str, agent: str) -> None:
    """Count a verified exchange against its trust and, if any, its agent;
    refuse it with 429 over a limit, 503 when this process is saturated."""
    per_trust = Attempts(
        "oauth.exchange", settings.workload_exchanges_per_trust, _EXCHANGE_WINDOW_S
    )
    gates = [(per_trust, trust)]
    if agent:
        per_agent = Attempts(
            "oauth.exchange.agent", settings.workload_exchanges_per_agent, _EXCHANGE_WINDOW_S
        )
        gates.append((per_agent, f"{trust}:{agent}"))
    # Counted as `admit` counts a sign-in, never released once admitted: one
    # refused at its recount (renewals of one agent racing) is released under
    # every gate it was counted under, so it spends none of the trust's room.
    try:
        async with slot():
            admitted = await admit(gates) is not None
    except Busy:
        _warn_once_a_minute(
            "exchange.busy", "oauth: too busy to count an exchange for trust %s; 503", trust
        )
        raise _unavailable() from None
    if admitted:
        return
    _warn_once_a_minute(
        f"exchange:{trust}",
        "oauth: exchange for trust %s%s over its limit; refused",
        trust,
        f" (agent {agent})" if agent else "",
    )
    raise OAuthError(
        "temporarily_unavailable",
        429,
        description="too many token exchanges for this trust or agent; retry later",
        headers={"Retry-After": "60"},
    )


async def _machine_tokens(
    m: machines.Machine,
    since: int,
    ends: int,
    how: dict[str, str],
    agent: str = "",
    own: str = "",
) -> dict[str, Any]:
    """The token for bot `m`, or for its `agent` on the agent's `own`
    namespace, good until `ends` (epoch seconds). `since` was taken before `m`
    was read, so a disable the read missed has a later cutoff, and revokes
    this token. An agent's token names the trust as its parent: disabling the
    trust ends every agent's. It also names what the agent was granted
    (`agents.with_grants`); a revoked grant caps the claim at once
    (`authz.claim_cap`). A platform-admin bot's token carries that role and
    every namespace, and no namespace roles, which the admin gates refuse."""
    from server.auth.revocation import subjects_revoked

    trust = machines.subject(m["name"])
    subject = agents.subject(m["name"], agent) if agent else trust
    if await subjects_revoked([trust, subject], issued_at=since):
        raise OAuthError("invalid_client", 401, description="this bot is revoked")
    admin = bool(m.get("platform_admin", False)) and not agent  # never an agent's
    if admin:
        from server.core.ch import get_client

        if not await machines.platform_admin_tokens_ready(get_client()):
            raise OAuthError(
                "temporarily_unavailable",
                503,
                description="platform-admin tokens require every replica to finish the rollout",
            )
    ns_roles: list[tuple[str, str]] = []
    if not admin:
        ns_roles = ([(own, "writer")] if own else []) + await machines.live_roles(m)
        if agent:
            ns_roles = await agents.with_grants(m, agent, ns_roles)
        if not ns_roles:
            raise OAuthError(
                "invalid_client", 401, description="none of this bot's namespaces is active"
            )
    jti, now = new_jti(), int(time.time())
    if ends - now < 30:  # its proof runs out (verify's leeway, a slow lookup)
        raise OAuthError("invalid_grant", description="the proof has run out")
    token = mint_token(
        subject=subject,
        org=m["org"],
        namespaces=["*"] if admin else [ns for ns, _ in ns_roles],
        roles=["platform_admin"] if admin else [],
        ns_roles=None if admin else dict(ns_roles),
        ttl_seconds=ends - since,  # dated `since`, it ends when its proof does
        is_service_account=True,
        jti=jti,
        issued_at=since,
        parent_subjects=[trust] if agent else None,
        parent_iats=[since] if agent else None,
    )
    await audit.emit(
        "auth.machine_token",
        org=m["org"],
        namespace="-",
        actor=subject,
        subject={
            "jti": jti,
            **how,
            "ttl_s": ends - since,
            **({"platform_admin": True} if admin else {}),
        },
    )
    return {"access_token": token, "token_type": "Bearer", "expires_in": ends - now}


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    from server.auth import device

    f = await form_body(request)
    grant_type = f.get("grant_type", "")
    iss = issuer(request)
    grants = {
        "authorization_code": _authorization_code_grant,
        "refresh_token": _refresh_token_grant,
        device.DEVICE_GRANT: device.device_code_grant,
        "client_credentials": lambda f: _client_credentials_grant(f, [f"{iss}/oauth/token", iss]),
        machines.TOKEN_EXCHANGE: _token_exchange_grant,
    }
    if grant_type not in grants:
        raise OAuthError("unsupported_grant_type")
    body = await grants[grant_type](f)  # Keeper not answering is _busy_response's
    return JSONResponse(body, headers={"Cache-Control": "no-store", SESSIONS_HEADER: "1"})


def _revocation_end(info: AuthInfo) -> datetime | None:
    """How long revoking this token must last: a session as long as its refresh
    tokens can, any other token until it expires (None: it never does)."""
    if info.session_id:
        return datetime.now(UTC) + timedelta(seconds=_REFRESH_TTL)
    return datetime.fromtimestamp(info.expires_at, UTC) if info.expires_at else None


@router.post("/oauth/revoke")
async def revoke_token(request: Request) -> Response:
    """RFC 7009 token revocation: `loom logout`. A refresh token ends its whole
    session, an access token Loom issued ends just that token's session. The
    answer is 200 whatever the token was (§2.2), so it tells a caller nothing."""
    from server.auth.jwt import TokenError, verify_token
    from server.auth.revocation import revoke

    f = await form_body(request)
    token_value = f.get("token", "")
    client_id = f.get("client_id", "")
    if not client_id:
        # A public client names itself (RFC 7009 §2.1); without it a refresh
        # token could not be matched, and logout would quietly do nothing.
        raise OAuthError("invalid_request", description="client_id required")
    grant: Grant | Device | None = None
    spent: Grant | None = None  # spent by this logout
    reason = "logout"
    try:
        grant = spent = await store.take_refresh(token_value, client_id)
    except (SessionRetired, CodeReplayed) as e:
        # A logout retried, an old copy (a later token was issued), or a
        # refusal that may not have ended the session; the audit keeps which.
        spent_how = "retired" if isinstance(e, SessionRetired) else "spent"
        grant, reason = e.grant, f"logout (token already {spent_how})"
    if grant is not None and await _ended(grant["family"]):
        # Ended already (a logout, a replay): only confirmed, so its first
        # reason stands. A token this logout took has nothing to replace it.
        if spent is not None:
            try:
                await store.retire_refresh(spent)
            except Exception:  # its session has ended: an unretired key refreshes nothing
                log.exception("oauth: logout could not retire a token of ended session")
            # The current token holds the IdP's current token, which whatever
            # ended the session (a replay, an old copy's logout) never had.
            await _revoke_upstream(spent)
        return Response(status_code=200)
    if grant is not None:
        try:
            await end_session(grant, reason)
            if spent is not None:  # nothing replaces it
                await store.retire_refresh(spent)
        except Exception as e:  # nothing durable landed: the client must not think it did
            log.exception("oauth: logout could not end session %s", grant["family"])
            raise _unavailable() from e
        await _revoke_upstream(grant)
        return Response(status_code=200)
    try:
        info = verify_token(token_value)
    except TokenError:
        return Response(status_code=200)  # unknown, expired or already revoked
    from server.auth.gateway import is_end_user

    if is_end_user(info):
        # Its `jti` is its issuer's choice, and the revocation list is
        # deployment-wide: an issuer would name another org's token or subject.
        # It lives minutes; its issuer stops signing for the user instead.
        return Response(status_code=200)
    # A session's access token ends its whole session; any other token itself.
    key = info.session_id or info.token_id
    if key:
        try:
            await revoke(
                key, expires_at=_revocation_end(info), reason="logout", revoked_by=info.subject
            )
        except Exception as e:  # not revoked: the client is told to retry, not that it worked
            log.exception("oauth: logout could not revoke %s", key)
            raise _unavailable() from e
        await audit.emit(
            "auth.session_revoked",
            org=info.org,
            namespace="-",
            actor=info.subject,
            subject={"family": info.session_id, "jti": info.token_id, "reason": "logout"},
        )
    return Response(status_code=200)


def _busy_response(request: Request, exc: Exception) -> JSONResponse:
    """Keeper could not answer a sign-in step: retryable, wherever it was."""
    log.warning("oauth: %s answered 503: %s", request.url.path, exc)
    return oauth_error_response(request, _unavailable())


def attach(app: Any) -> None:
    """Mount sign-in wherever there is something to sign in to, and MCP
    connect (dynamic client registration, an opt-in surface) only when enabled."""
    from server.auth import device, oidc

    if settings.auth_actually_required() or settings.oauth_enabled:
        oidc.validate_config()  # fail fast if provider=oidc but IdP config missing
        app.add_exception_handler(OAuthError, oauth_error_response)
        app.add_exception_handler(claims.Busy, _busy_response)
        app.include_router(router)
        app.include_router(device.router)
    if settings.oauth_enabled:
        app.include_router(connect_router)
