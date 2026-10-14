"""OAuth 2.1 Authorization Server for MCP one-click connect (opt-in).

When `LOOM_OAUTH_ENABLED=true`, loom advertises OAuth discovery and serves the
authorization-code + PKCE flow MCP clients use to connect via browser sign-in
instead of a pasted JWT:

  client → /.well-known discovery → /oauth/register (dynamic client
  registration) → GET /oauth/authorize (sign-in) → redirect with code →
  POST /oauth/token (PKCE verify → loom JWT access token + refresh token).

The access token is a normal loom JWT minted subject-aligned to the user, so it
verifies through the existing JWKS path and inherits the user's namespace
grants. Public clients only (PKCE replaces a client secret), S256 only. The
sign-in step is password by default, or delegated to an external OIDC provider
when `LOOM_OAUTH_LOGIN_PROVIDER=oidc` (see oidc.py). Grants persist in
`loom.oauth_state`, durable across restarts and replicas.
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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from server.auth.jwt import mint_token
from server.settings import settings

log = logging.getLogger(__name__)

router = APIRouter()

_ACCESS_TTL = 3600  # 1h access token
_CODE_TTL = 60  # auth code: short-lived, single-use
_REFRESH_TTL = 30 * 86400  # 30d refresh token
_PENDING_TTL = 600  # OIDC pending-login record: time to complete IdP sign-in


# --------------------------------------------------------------------------
# Grant store — durable (ClickHouse) by default; in-memory for tests.
#
# Async interface (both impls):
#   save_client(client_id, redirect_uris, client_name) / get_client(client_id)
#   save_code(code, grant)   / find_code(code), consume_code(code, grant)
#   save_refresh(token, gr)  / find_refresh(token, client_id),
#     consume_refresh(token, grant) — single-use, consumed once the exchange
#     is decided; a refresh is bound to the issuee: another client_id finds
#     nothing, so a wrong presenter can't burn it.
# Codes and refresh tokens are bearer secrets, so the store keys them by
# _token_id(token) (sha256 hex), never the token itself: a row read out of
# loom.oauth_state (a backup, a SELECT grant) is not a working credential.
# A "grant" is the dict {client_id, redirect_uri, code_challenge, subject, org,
# namespaces, roles, exp(int epoch)} (code) or the subset for refresh.
# expires_at is stored as a Unix epoch int (UInt32), NOT a DateTime: integers
# have no timezone, so they round-trip through CH without the naive-datetime
# local-vs-UTC shift that would mark every fresh code instantly expired.
# --------------------------------------------------------------------------
def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _CHStore:
    """Durable OAuth state in loom.oauth_state — survives restart + replicas.

    Single-use is a consumed=1 ReplacingMergeTree version; reads take FINAL +
    consumed=0 + unexpired, with sequential consistency, so a replica that has
    not seen a consume cannot replay the credential. Finding and consuming are
    two steps (the exchange is decided in between), so /oauth/token keeps two
    concurrent exchanges of one credential apart with a Keeper claim
    (`oauth_claims`); without a Keeper both can succeed.
    """

    _T = "loom.oauth_state"

    def _ch(self) -> Any:
        from server.core.ch import get_client

        return get_client()

    async def save_client(self, client_id: str, redirect_uris: list[str], name: str) -> None:
        await self._ch().insert(
            self._T,
            [
                {
                    "record_type": "client",
                    "id": client_id,
                    "redirect_uris": list(redirect_uris),
                    "client_name": name,
                    "version": time.time_ns(),
                }
            ],
        )

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        row = await self._ch().query_one(
            f"SELECT redirect_uris, client_name FROM {self._T} FINAL "
            "WHERE record_type = 'client' AND id = {id:String} AND consumed = 0 LIMIT 1",
            params={"id": client_id},
        )
        if not row:
            return None
        return {"redirect_uris": list(row["redirect_uris"]), "client_name": row["client_name"]}

    async def save_code(self, code: str, g: dict[str, Any]) -> None:
        await self._ch().insert(
            self._T,
            [
                {
                    "record_type": "code",
                    "id": _token_id(code),
                    "client_id": g["client_id"],
                    "redirect_uri": g["redirect_uri"],
                    "code_challenge": g["code_challenge"],
                    "subject": g["subject"],
                    "org": g["org"],
                    "namespaces": list(g["namespaces"]),
                    "roles": list(g["roles"]),
                    "expires_at": int(g["exp"]),
                    "version": time.time_ns(),
                }
            ],
        )

    async def save_refresh(self, token: str, g: dict[str, Any]) -> None:
        await self._ch().insert(
            self._T,
            [
                {
                    "record_type": "refresh",
                    "id": _token_id(token),
                    "client_id": g["client_id"],
                    "subject": g["subject"],
                    "org": g["org"],
                    "namespaces": list(g["namespaces"]),
                    "roles": list(g["roles"]),
                    "expires_at": int(g["exp"]),
                    "version": time.time_ns(),
                }
            ],
        )

    async def find_code(self, code: str) -> dict[str, Any] | None:
        return await self._find("code", _token_id(code))

    async def find_refresh(self, token: str, client_id: str) -> dict[str, Any] | None:
        return await self._find("refresh", _token_id(token), client_id)

    async def consume_code(self, code: str, grant: dict[str, Any]) -> None:
        await self._consume("code", _token_id(code), grant)

    async def consume_refresh(self, token: str, grant: dict[str, Any]) -> None:
        await self._consume("refresh", _token_id(token), grant)

    async def _find(
        self, rtype: str, rid: str, client_id: str | None = None
    ) -> dict[str, Any] | None:
        from server.core.ch import own_write_reads

        with own_write_reads():
            row = await self._ch().query_one(
                f"""SELECT client_id, redirect_uri, code_challenge, subject, org,
                           namespaces, roles, expires_at
                    FROM {self._T} FINAL
                    WHERE record_type = {{t:String}} AND id = {{id:String}} AND consumed = 0
                    LIMIT 1""",
                params={"t": rtype, "id": rid},
            )
        if not row:
            return None
        exp = int(row.get("expires_at") or 0)
        if exp and exp < int(time.time()):
            return None
        if client_id is not None and row["client_id"] != client_id:
            return None
        return {
            "client_id": row["client_id"],
            "redirect_uri": row.get("redirect_uri", ""),
            "code_challenge": row.get("code_challenge", ""),
            "subject": row["subject"],
            "org": row["org"],
            "namespaces": list(row["namespaces"]),
            "roles": list(row["roles"]),
            "exp": exp or (int(time.time()) + _REFRESH_TTL),
        }

    async def _consume(self, rtype: str, rid: str, grant: dict[str, Any]) -> None:
        """Mark consumed: a new full-row version (RMT collapses to it)."""
        await self._ch().insert(
            self._T,
            [
                {
                    "record_type": rtype,
                    "id": rid,
                    "client_id": grant["client_id"],
                    "redirect_uri": grant["redirect_uri"],
                    "code_challenge": grant["code_challenge"],
                    "subject": grant["subject"],
                    "org": grant["org"],
                    "namespaces": list(grant["namespaces"]),
                    "roles": list(grant["roles"]),
                    "expires_at": int(grant["exp"]),
                    "consumed": 1,
                    "version": time.time_ns(),
                }
            ],
        )

    async def save_pending(self, state: str, data: dict[str, Any]) -> None:
        # OIDC in-flight login: stash the MCP client's params + nonce keyed by
        # the IdP `state` nonce. Single-use, short TTL; the JSON blob rides the
        # client_name column (unused for non-client records).
        await self._ch().insert(
            self._T,
            [
                {
                    "record_type": "oidc_pending",
                    "id": state,
                    "client_name": json.dumps(data),
                    "expires_at": int(time.time()) + _PENDING_TTL,
                    "version": time.time_ns(),
                }
            ],
        )

    async def take_pending(self, state: str) -> dict[str, Any] | None:
        ch = self._ch()
        row = await ch.query_one(
            f"SELECT client_name, expires_at FROM {self._T} FINAL WHERE "
            "record_type = 'oidc_pending' AND id = {id:String} AND consumed = 0 LIMIT 1",
            params={"id": state},
        )
        if not row:
            return None
        exp = int(row.get("expires_at") or 0)
        if exp and exp < int(time.time()):
            return None
        await ch.insert(
            self._T,
            [
                {
                    "record_type": "oidc_pending",
                    "id": state,
                    "client_name": row["client_name"],
                    "expires_at": exp,
                    "consumed": 1,
                    "version": time.time_ns(),
                }
            ],
        )
        data: dict[str, Any] = json.loads(row["client_name"])
        return data


_store: Any = _CHStore()


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


async def _form_body(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body (the OAuth wire format)
    without pulling in python-multipart — last value wins per key. A body
    that is not UTF-8 is the client's error (400 invalid_request), not a 500."""
    try:
        raw = (await request.body()).decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "body is not UTF-8"},
        ) from e
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def authorization_server_metadata(issuer: str) -> dict[str, Any]:
    """Authorization-server metadata — what the client needs to drive the flow."""
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["loom"],
    }


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


@router.get("/.well-known/oauth-protected-resource")
async def prm_metadata(request: Request) -> JSONResponse:
    iss = issuer(request)
    return JSONResponse(protected_resource_metadata(iss, f"{iss}/mcp"))


_MAX_REDIRECT_URIS = 10
_MAX_URI_CHARS = 2048
# Schemes a browser would execute or read locally instead of handing a code
# to a client.
_BAD_SCHEMES = frozenset({"javascript", "data", "file", "vbscript", "blob", "about"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


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
@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError as e:  # JSONDecodeError and UnicodeDecodeError both are
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_client_metadata",
                "error_description": "the registration body must be a JSON object",
            },
        ) from e
    redirect_uris = body.get("redirect_uris") if isinstance(body, dict) else None
    if (
        not isinstance(redirect_uris, list)
        or not 0 < len(redirect_uris) <= _MAX_REDIRECT_URIS
        or not all(isinstance(u, str) and valid_redirect_uri(u) for u in redirect_uris)
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_redirect_uri",
                "error_description": (
                    "redirect_uris: 1-10 absolute URIs without a fragment; "
                    "https, http to a loopback host, or an app scheme"
                ),
            },
        )
    client_id = "loom-" + secrets.token_urlsafe(16)
    uris = list(redirect_uris)
    name = "".join(c for c in str(body.get("client_name") or "MCP client") if c.isprintable())
    await _store.save_client(client_id, uris, name[:120] or "MCP client")
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
    client: dict[str, Any] | None = await _store.get_client(client_id)
    if client is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_client"})
    if redirect_uri not in client["redirect_uris"]:
        raise HTTPException(status_code=400, detail={"error": "invalid_redirect_uri"})
    if code_challenge_method != "S256" or not code_challenge:
        # PKCE S256 is mandatory — no plaintext / no missing challenge.
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "S256 PKCE required"},
        )
    return client


@router.get("/oauth/authorize", response_class=HTMLResponse)
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
        raise HTTPException(status_code=400, detail={"error": "unsupported_response_type"})
    client = await _validate_authorize_params(
        client_id, redirect_uri, code_challenge, code_challenge_method
    )
    # Delegate the human sign-in to an external IdP when configured; loom still
    # mints the token after the callback maps the verified email to an account.
    if settings.oauth_login_provider == "oidc":
        return await _start_oidc_login(
            client_id,
            redirect_uri,
            code_challenge,
            code_challenge_method,
            state,
        )
    client_name = html.escape(client["client_name"])
    # Dynamic registration lets anyone name a client anything, so the page
    # also says where the authorization goes — the part a phisher cannot fake.
    destination = html.escape(urlsplit(redirect_uri).netloc or redirect_uri.split(":", 1)[0])
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
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>Connect Loom</title>
<style>body{{font-family:system-ui;max-width:24rem;margin:4rem auto;padding:0 1rem}}
input{{display:block;width:100%;padding:.6rem;margin:.4rem 0;box-sizing:border-box}}
button{{padding:.6rem 1rem;width:100%;background:#111;color:#fff;border:0;border-radius:6px}}
</style></head><body>
<h2>Connect <b>{client_name}</b> to Loom</h2>
<p>Sign in to authorize this client to access your memories.
After you sign in, Loom sends the authorization to <b>{destination}</b>.</p>
<form method="post" action="/oauth/authorize">{hidden}
<input name="identifier" placeholder="email or namespace"
       autocomplete="username" required>
<input name="password" type="password" placeholder="password"
       autocomplete="current-password" required>
<button type="submit">Authorize</button>
</form></body></html>"""
    )


@router.post("/oauth/authorize")
async def authorize_submit(request: Request) -> RedirectResponse:
    f = await _form_body(request)
    client_id = f.get("client_id", "")
    redirect_uri = f.get("redirect_uri", "")
    code_challenge = f.get("code_challenge", "")
    code_challenge_method = f.get("code_challenge_method", "S256")
    state = f.get("state", "")
    await _validate_authorize_params(client_id, redirect_uri, code_challenge, code_challenge_method)
    from server.auth.attempts import client_address
    from server.auth.login import LoginThrottled, resolve_user_credentials

    try:
        resolved = await resolve_user_credentials(
            f.get("identifier", ""), f.get("password", ""), address=client_address(request)
        )
    except LoginThrottled as e:
        raise HTTPException(
            status_code=429,
            detail={"error": "temporarily_unavailable"},
            headers={"Retry-After": "900"},
        ) from e
    if resolved is None:
        # Bounce back to the form with an error (don't leak which field).
        raise HTTPException(status_code=401, detail={"error": "access_denied"})
    subject, org, namespaces, roles = resolved
    code = secrets.token_urlsafe(32)
    await _store.save_code(
        code,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "subject": subject,
            "org": org,
            "namespaces": namespaces,
            "roles": roles,
            "exp": int(time.time()) + _CODE_TTL,
        },
    )
    params = {"code": code}
    if state:
        params["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(url=f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


# --------------------------------------------------------------------------
# OIDC-brokered login: delegate the human sign-in to an external IdP, then map
# the verified email to a loom account and resume loom's own code issuance.
# --------------------------------------------------------------------------
def _oidc_callback_url() -> str:
    # From trusted config only (validate_config requires oauth_issuer under
    # oidc), never the request Host header — this URL is registered at the IdP
    # and a spoofed Host must not be able to redirect the code elsewhere.
    return f"{settings.oauth_issuer.rstrip('/')}/oauth/callback"


async def _start_oidc_login(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str,
) -> RedirectResponse:
    from server.auth import oidc

    oidc_state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    await _store.save_pending(
        oidc_state,
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "mcp_state": state,
            "nonce": nonce,
        },
    )
    url = await oidc.build_authorize_url(
        redirect_uri=_oidc_callback_url(), state=oidc_state, nonce=nonce
    )
    return RedirectResponse(url=url, status_code=302)


@router.get("/oauth/callback")
async def oidc_callback(request: Request) -> RedirectResponse:
    from server.auth import oidc
    from server.auth.login import jit_provision_for_email, resolve_identity_for_email

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state:
        raise HTTPException(status_code=400, detail={"error": "invalid_request"})
    pending = await _store.take_pending(state)  # single-use; CSRF guard
    if pending is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_state"})
    try:
        tokens = await oidc.exchange_code(code=code, redirect_uri=_oidc_callback_url())
        claims = await oidc.verify_id_token(tokens.get("id_token", ""), nonce=pending["nonce"])
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        # The IdP is unreachable or refused the exchange: not the user's fault,
        # and its response body stays in the log, not the browser.
        log.warning("oidc callback: identity provider exchange failed: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail={"error": "temporarily_unavailable"}) from e
    except Exception as e:  # any other verify failure refuses the login (re-raised)
        log.warning("oidc callback: id_token refused: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail={"error": "oidc_error"}) from e
    email = oidc.verified_email(claims)
    if not email:
        raise HTTPException(
            status_code=403,
            detail={"error": "access_denied", "error_description": "verified email required"},
        )
    resolved = await resolve_identity_for_email(email)
    if resolved is None:
        # No account — auto-provision one if JIT is on (else stay fail-closed).
        resolved = await jit_provision_for_email(email)
    if resolved is None:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "access_denied",
                "error_description": "no loom account for this email",
            },
        )
    subject, org, namespaces, roles = resolved
    # Resume loom's own flow: mint a code bound to the MCP client's PKCE.
    code_out = secrets.token_urlsafe(32)
    await _store.save_code(
        code_out,
        {
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "code_challenge": pending["code_challenge"],
            "subject": subject,
            "org": org,
            "namespaces": namespaces,
            "roles": roles,
            "exp": int(time.time()) + _CODE_TTL,
        },
    )
    params = {"code": code_out}
    if pending.get("mcp_state"):
        params["state"] = pending["mcp_state"]
    target = pending["redirect_uri"]
    sep = "&" if "?" in target else "?"
    return RedirectResponse(url=f"{target}{sep}{urlencode(params)}", status_code=302)


# --------------------------------------------------------------------------
# Token endpoint
# --------------------------------------------------------------------------
async def _issue_tokens(grant: dict[str, Any], client_id: str) -> dict[str, Any]:
    access = mint_token(
        subject=grant["subject"],
        org=grant["org"],
        namespaces=list(grant["namespaces"]),
        roles=list(grant["roles"]),
        ttl_seconds=_ACCESS_TTL,
        is_service_account=False,
    )
    refresh = secrets.token_urlsafe(32)
    await _store.save_refresh(
        refresh,
        {
            "client_id": client_id,
            "subject": grant["subject"],
            "org": grant["org"],
            "namespaces": list(grant["namespaces"]),
            "roles": list(grant["roles"]),
            "exp": int(time.time()) + _REFRESH_TTL,
        },
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": _ACCESS_TTL,
        "refresh_token": refresh,
        "scope": "loom",
    }


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    f = await _form_body(request)
    grant_type = f.get("grant_type", "")
    code = f.get("code", "")
    redirect_uri = f.get("redirect_uri", "")
    client_id = f.get("client_id", "")
    code_verifier = f.get("code_verifier", "")
    refresh_token = f.get("refresh_token", "")
    if grant_type == "authorization_code":
        async with _one_exchange("code", code):
            grant = await _store.find_code(code)
            if grant is None:
                raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
            decided = _decide_code(grant, client_id, redirect_uri, code_verifier)
            issued = await _used_up_unless_retryable(
                _issued(decided, client_id), lambda: _store.consume_code(code, grant)
            )
        return JSONResponse(issued)

    if grant_type == "refresh_token":
        # A public client identifies itself on refresh (RFC 6749 §6), and the
        # token refreshes only for the client it was issued to; a mismatch
        # finds nothing and leaves the token for its owner.
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_request", "error_description": "client_id required"},
            )
        async with _one_exchange("refresh", refresh_token):
            grant = await _store.find_refresh(refresh_token, client_id)
            if grant is None:
                raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
            decided = _decide_refresh(grant)
            issued = await _used_up_unless_retryable(
                _issued(decided, grant["client_id"]),
                lambda: _store.consume_refresh(refresh_token, grant),
            )
        return JSONResponse(issued)

    raise HTTPException(status_code=400, detail={"error": "unsupported_grant_type"})


@asynccontextmanager
async def _one_exchange(record_type: str, credential: str) -> AsyncIterator[None]:
    """Hold the credential's Keeper claim while its exchange runs
    (`oauth_claims`). A concurrent exchange of it is told to retry, not that
    the credential is used: it may be the owner retrying after a reply that
    never arrived."""
    from server.auth import oauth_claims
    from server.core.ch import get_client

    ch = get_client()
    key = f"{record_type}:{_token_id(credential)}"
    try:
        outcome, holder = await oauth_claims.claim(ch, key)
    except Exception as e:  # an exchange Keeper could not keep apart does not run
        raise _unavailable("exchange claim", e) from e
    if outcome is oauth_claims.Claim.TAKEN:
        raise _unavailable("exchange claim", RuntimeError("another exchange holds it"))
    try:
        yield
    finally:
        if outcome is oauth_claims.Claim.HELD:
            await oauth_claims.release(ch, key, holder)


async def _issued(decided: Awaitable[dict[str, Any]], client_id: str) -> dict[str, Any]:
    """The token response for the grant the exchange decided on, its refresh
    token already saved."""
    return await _issue_tokens(await decided, client_id)


async def _used_up_unless_retryable(
    issuing: Awaitable[dict[str, Any]], consume: Callable[[], Awaitable[None]]
) -> dict[str, Any]:
    """Await the exchange's tokens, then use the code or refresh token up. A
    refusal uses it up too; a failure leaves it (503), so the client's retry
    works. Only the decision and the issue are bounded
    (`oauth_claims.EXCHANGE_TIMEOUT_S`): the consume is the commit, and a
    cancelled one may still land. Issued before the consume, a token the
    client never receives is the worst a failed consume leaves."""
    from server.auth.oauth_claims import EXCHANGE_TIMEOUT_S
    from server.core.ch import statement_timeout

    try:
        async with asyncio.timeout(EXCHANGE_TIMEOUT_S):
            issued = await issuing
    except HTTPException as e:
        if e.status_code != 503:
            with statement_timeout(5):
                await consume()
        raise
    except Exception as e:  # a decision or an issue that did not finish uses nothing up
        raise _unavailable("exchange", e) from e
    with statement_timeout(5):
        await consume()
    return issued


async def _decide_code(
    grant: dict[str, Any], client_id: str, redirect_uri: str, code_verifier: str
) -> dict[str, Any]:
    if grant["client_id"] != client_id or grant["redirect_uri"] != redirect_uri:
        raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
    if not verify_pkce_s256(code_verifier, grant["code_challenge"]):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_grant", "error_description": "PKCE failed"},
        )
    code_issued_at = int(grant["exp"]) - _CODE_TTL
    return {**grant, "namespaces": await _mint_namespaces(grant, code_issued_at)}


async def _decide_refresh(grant: dict[str, Any]) -> dict[str, Any]:
    """The grant a refresh mints from. The grant is a sign-in snapshot; each
    refresh re-checks the account is still active and re-derives its roles,
    so neither a released account nor a removed host rides a rolling 30-day
    chain, and drops a namespace a grant was taken away on for good: the new
    token's iat would pass the cutoff, and so would every later refresh."""
    from server.auth.login import refreshed_roles

    # exp is always saved as mint time + _REFRESH_TTL.
    issued_at = int(grant["exp"]) - _REFRESH_TTL
    try:
        roles = await refreshed_roles(grant["subject"], grant["org"], list(grant["roles"]))
    except Exception as e:  # an account that could not be read mints nothing
        raise _unavailable("account read", e, grant) from e
    if roles is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
    return {**grant, "roles": roles, "namespaces": await _mint_namespaces(grant, issued_at)}


def _unavailable(what: str, e: Exception, grant: dict[str, Any] | None = None) -> HTTPException:
    """The retryable refusal of an exchange whose `what` failed."""
    who = f" for {grant['subject']!r} in {grant['org']!r}" if grant else ""
    log.warning("oauth: %s failed%s: %s", what, who, e)
    return HTTPException(
        status_code=503,
        detail={"error": "temporarily_unavailable"},
        headers={"Retry-After": "5"},
    )


async def _mint_namespaces(grant: dict[str, Any], issued_at: int) -> list[str]:
    """The namespaces a token minted from `grant` may claim, `issued_at` being
    when the grant was decided. Refused when the subject was revoked since;
    without the namespaces a grant was taken away on since, unless restricted
    and still granted (a restricted namespace is read only by a token naming
    it, and the live grant is checked on every call). Read from ClickHouse:
    a token minted past a revocation this process has not seen keeps it."""
    from server.auth.login import widen_with_restricted_grants
    from server.auth.revocation import ended_claims, subject_revoked

    org, subject = grant["org"], grant["subject"]
    namespaces = list(grant["namespaces"])
    try:
        if await subject_revoked(subject, issued_at=issued_at):
            raise HTTPException(status_code=400, detail={"error": "invalid_grant"})
        ended = await ended_claims(org, subject, namespaces, issued_at=issued_at)
        if ended:
            ended -= set(await widen_with_restricted_grants(subject, org, [], strict=True))
    except HTTPException:
        raise
    except Exception as e:  # no token is minted past a revocation it could not read
        raise _unavailable("mint-time read", e, grant) from e
    return [ns for ns in namespaces if ns not in ended]


def attach(app: Any) -> None:
    """Mount the OAuth AS only when enabled (opt-in authz surface)."""
    if settings.oauth_enabled:
        from server.auth import oidc

        oidc.validate_config()  # fail fast if provider=oidc but IdP config missing
        app.include_router(router)
