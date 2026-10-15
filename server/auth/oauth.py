"""OAuth 2.1 Authorization Server: sign-in for the CLI, the SDK and MCP clients.

`router` is mounted whenever the deployment requires auth. It serves the
device grant `loom login` uses (RFC 8628, see device.py), the token endpoint,
token revocation (RFC 7009), the identity provider callback and the
dashboard's SSO sign-in. `connect_router` adds MCP one-click connect when
`LOOM_OAUTH_ENABLED=true`:

  client → /.well-known discovery → /oauth/register (dynamic client
  registration) → GET /oauth/authorize (sign-in) → redirect with code →
  POST /oauth/token (PKCE verify → loom JWT access token + refresh token).

The access token is a normal loom JWT minted subject-aligned to the user, so it
verifies through the existing JWKS path and inherits the user's namespace
grants. Public clients only (PKCE replaces a client secret), S256 only. The
sign-in step is password by default, or delegated to an external OIDC provider
when `LOOM_OAUTH_LOGIN_PROVIDER=oidc` (see oidc.py). Grants persist in
`loom.oauth_state`, durable across restarts and replicas.

Each sign-in starts a session `family`. Its access tokens carry the family as
their `sid` and its refresh tokens rotate within it, so revoking the family in
`loom.revoked_tokens` ends the whole session: on logout, when a spent refresh
token or code comes back (someone else holds a copy), and when the identity
provider refuses the user at refresh.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from server.auth.attempts import Attempts, client_address
from server.auth.delegation import SESSION_TTL
from server.auth.jwt import AuthInfo, mint_token, new_jti
from server.core.secrets_at_rest import seal, unseal
from server.observability import audit
from server.settings import settings

log = logging.getLogger(__name__)

router = APIRouter()
connect_router = APIRouter()

_ACCESS_TTL = 3600  # 1h access token
_CODE_TTL = 60  # auth code: short-lived, single-use
_REFRESH_TTL = SESSION_TTL  # 30d refresh token: a session's horizon
_PENDING_TTL = 600  # OIDC pending-login record: time to complete IdP sign-in
_UPSTREAM_PURPOSE = "oidc refresh token"


class CodeReplayed(Exception):
    """A single-use code or refresh token was presented after it was spent,
    `spent_at` (unix seconds) ago."""

    def __init__(self, grant: dict[str, Any], spent_at: float) -> None:
        super().__init__("spent credential presented again")
        self.grant = grant
        self.spent_at = spent_at


# --------------------------------------------------------------------------
# Grant store — durable (ClickHouse) by default; in-memory for tests.
#
# Async interface (both impls):
#   save_client(client_id, redirect_uris, client_name) / get_client(client_id)
#   save_code(code, grant)   / take_code(code, client_id) — single-use, and
#     like take_refresh bound to the issuee
#   save_refresh(token, gr)  / take_refresh(token, client_id) — rotating
#     (single-use) and bound to the issuee: another client_id gets None and
#     the token is left unconsumed, so a wrong presenter can't burn it.
#   take_* raise CodeReplayed when the code or token was already spent.
#   save_device(device) / device_by_user_code(hash) / take_device(code) — the
#     device grant (device.py); take_device consumes only a decided grant.
# Codes, refresh tokens and device codes are bearer secrets, so the store
# keys them by token_id(token) (sha256 hex), never the token itself: a row
# read out of loom.oauth_state (a backup, a SELECT grant) is not a working
# credential. User codes are short enough to search, so device.py keys them
# with the server's secret. The identity provider's refresh token has to be
# sent back to it, so it is sealed (secrets_at_rest).
# A "grant" is the dict {client_id, redirect_uri, code_challenge, subject, org,
# namespaces, roles, exp(int epoch), family, upstream_refresh} (code) or the
# subset for refresh.
# expires_at is stored as a Unix epoch int (UInt32), NOT a DateTime: integers
# have no timezone, so they round-trip through CH without the naive-datetime
# local-vs-UTC shift that would mark every fresh code instantly expired.
# --------------------------------------------------------------------------
def token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _CHStore:
    """Durable OAuth state in loom.oauth_state — survives restart + replicas.

    Single-use is a consumed=1 ReplacingMergeTree version; reads take FINAL
    and check consumed + expiry themselves.

    Known limit: take_* is read-then-mark, not atomic: two presenters of the same
    code or refresh token racing within one round trip can both succeed. A code
    is useless without its PKCE verifier and lives 60 s; a refresh token is
    bound to its client_id. device.signed_in's "the first person to sign in
    owns the code" is the same read-then-write. An insert_deduplication_token
    would drop the second consume row but tell neither caller, so it is no
    guard; the upgrade is a KeeperMap claim key under keeper_map_strict_mode
    (the migration lock's mechanism), whose second INSERT fails.
    """

    _T = "loom.oauth_state"
    _GRANT_COLUMNS = (
        "client_id, redirect_uri, code_challenge, subject, org, namespaces, roles, "
        "expires_at, consumed, family, upstream_refresh"
    )
    _DEVICE_COLUMNS = (
        "id, subject, org, namespaces, roles, expires_at, family, upstream_refresh, "
        "user_code, status, address, ticket"
    )

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

    def _grant_row(self, rtype: str, rid: str, g: dict[str, Any], consumed: int) -> dict[str, Any]:
        return {
            "record_type": rtype,
            "id": rid,
            "client_id": g["client_id"],
            "redirect_uri": g.get("redirect_uri", ""),
            "code_challenge": g.get("code_challenge", ""),
            "subject": g["subject"],
            "org": g["org"],
            "namespaces": list(g["namespaces"]),
            "roles": list(g["roles"]),
            "expires_at": int(g["exp"]),
            "family": g.get("family", ""),
            "upstream_refresh": seal(_UPSTREAM_PURPOSE, g.get("upstream_refresh", "")),
            "consumed": consumed,
            "version": time.time_ns(),
        }

    async def save_code(self, code: str, g: dict[str, Any]) -> None:
        await self._ch().insert(self._T, [self._grant_row("code", token_id(code), g, 0)])

    async def save_refresh(self, token: str, g: dict[str, Any]) -> None:
        await self._ch().insert(self._T, [self._grant_row("refresh", token_id(token), g, 0)])

    async def take_code(self, code: str, client_id: str) -> dict[str, Any] | None:
        return await self._take("code", token_id(code), client_id)

    async def take_refresh(self, token: str, client_id: str) -> dict[str, Any] | None:
        return await self._take("refresh", token_id(token), client_id)

    async def _take(self, rtype: str, rid: str, client_id: str) -> dict[str, Any] | None:
        row = await self._ch().query_one(
            f"""SELECT {self._GRANT_COLUMNS}, version FROM {self._T} FINAL
                WHERE record_type = {{t:String}} AND id = {{id:String}} LIMIT 1""",
            params={"t": rtype, "id": rid},
        )
        if not row or int(row["expires_at"] or 0) < int(time.time()):
            return None
        if row["client_id"] != client_id:
            return None  # checked before the consume: a wrong presenter can't burn it
        grant = {
            "client_id": row["client_id"],
            "redirect_uri": row["redirect_uri"],
            "code_challenge": row["code_challenge"],
            "subject": row["subject"],
            "org": row["org"],
            "namespaces": list(row["namespaces"]),
            "roles": list(row["roles"]),
            "exp": int(row["expires_at"]),
            "family": row["family"],
            "upstream_refresh": unseal(_UPSTREAM_PURPOSE, row["upstream_refresh"]),
        }
        if row["consumed"]:
            raise CodeReplayed(grant, int(row["version"]) / 1e9)  # the consume row's time_ns
        await self._ch().insert(self._T, [self._grant_row(rtype, rid, grant, 1)])
        return grant

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

    def _device_row(self, d: dict[str, Any], consumed: int) -> dict[str, Any]:
        return {
            "record_type": "device",
            "id": d["id"],
            "client_id": d["client_id"],
            "subject": d["subject"],
            "org": d["org"],
            "namespaces": list(d["namespaces"]),
            "roles": list(d["roles"]),
            "expires_at": int(d["exp"]),
            "family": d["family"],
            "upstream_refresh": seal(_UPSTREAM_PURPOSE, d["upstream_refresh"]),
            "user_code": d["user_code"],
            "status": d["status"],
            "address": d["address"],
            "ticket": d["ticket"],
            "consumed": consumed,
            "version": time.time_ns(),
        }

    def _device(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "subject": row["subject"],
            "org": row["org"],
            "namespaces": list(row["namespaces"]),
            "roles": list(row["roles"]),
            "exp": int(row["expires_at"]),
            "family": row["family"],
            "upstream_refresh": unseal(_UPSTREAM_PURPOSE, row["upstream_refresh"]),
            "user_code": row["user_code"],
            "status": row["status"],
            "address": row["address"],
            "ticket": row["ticket"],
        }

    async def save_device(self, d: dict[str, Any]) -> None:
        await self._ch().insert(self._T, [self._device_row(d, 0)])

    async def device_by_user_code(self, user_code_id: str) -> dict[str, Any] | None:
        """The live, undecided device grant whose user code hashes to this."""
        row = await self._ch().query_one(
            f"""SELECT client_id, {self._DEVICE_COLUMNS} FROM {self._T} FINAL
                WHERE record_type = 'device' AND user_code = {{u:String}}
                  AND consumed = 0 AND status = 'pending'
                  AND expires_at >= toUInt32(now())
                LIMIT 1""",
            params={"u": user_code_id},
        )
        return self._device(row) if row else None

    async def take_device(self, device_code: str) -> dict[str, Any] | None:
        """The device grant as the polling client sees it. An approved or
        denied grant is consumed by this read; a pending one is not."""
        row = await self._ch().query_one(
            f"""SELECT client_id, consumed, version, {self._DEVICE_COLUMNS} FROM {self._T} FINAL
                WHERE record_type = 'device' AND id = {{id:String}} LIMIT 1""",
            params={"id": token_id(device_code)},
        )
        if not row:
            return None
        d = self._device(row)
        if d["exp"] < int(time.time()):
            return d  # expired: the poller hears so, spent or not
        if row["consumed"]:
            raise CodeReplayed(d, int(row["version"]) / 1e9)
        if d["status"] != "pending":
            await self._ch().insert(self._T, [self._device_row(d, 1)])
        return d


store: Any = _CHStore()


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
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "body is not UTF-8"},
        ) from e
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
        "grant_types_supported": [DEVICE_GRANT, "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
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
# put under another site's overlay cannot be clickjacked into approving.
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def html_page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
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
        headers=_PAGE_HEADERS,
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
        raise HTTPException(status_code=400, detail={"error": "unsupported_response_type"})
    client = await _validate_authorize_params(
        client_id, redirect_uri, code_challenge, code_challenge_method
    )
    # Delegate the human sign-in to an external IdP when configured; loom still
    # mints the token after the callback maps the verified email to an account.
    if settings.oauth_login_provider == "oidc":
        return await start_oidc_login(
            "mcp",
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
        raise HTTPException(status_code=404, detail={"error": "password_sign_in_disabled"})
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
    return await _redirect_with_code(
        {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": code_challenge},
        resolved,
        upstream_refresh="",
        state=state,
    )


async def _redirect_with_code(
    client: dict[str, str],
    identity: tuple[str, str, list[str], list[str]],
    *,
    upstream_refresh: str,
    state: str,
) -> RedirectResponse:
    """Mint the MCP client's single-use code for a signed-in user and send the
    browser back to the client with it."""
    subject, org, namespaces, roles = identity
    code = secrets.token_urlsafe(32)
    await store.save_code(
        code,
        {
            **client,
            "subject": subject,
            "org": org,
            "namespaces": namespaces,
            "roles": roles,
            "exp": int(time.time()) + _CODE_TTL,
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
def _state_cookie(state: str) -> tuple[str, str]:
    """(cookie name, value) for one IdP sign-in."""
    digest = token_id(state)
    return f"loom_oidc_{digest[:16]}", digest


_sso_starts_by_address = Attempts(30, 900)


async def start_oidc_login(kind: str, **data: str) -> RedirectResponse:
    """Send the browser to the IdP; `data` comes back to the callback."""
    from server.auth import oidc

    oidc_state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    await store.save_pending(
        oidc_state, {**data, "kind": kind, "nonce": nonce, "code_verifier": verifier}
    )
    url = await oidc.build_authorize_url(
        redirect_uri=_oidc_callback_url(),
        state=oidc_state,
        nonce=nonce,
        code_challenge=_b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        offline=kind != "dashboard",  # the dashboard's token never refreshes
    )
    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        *_state_cookie(oidc_state),
        max_age=_PENDING_TTL,
        path="/oauth/callback",
        secure=settings.oauth_issuer.startswith("https://"),
        httponly=True,
        samesite="lax",  # sent on the IdP's top-level redirect back
    )
    return response


@router.get("/oauth/sso")
async def dashboard_sso(request: Request) -> RedirectResponse:
    """The dashboard's "Sign in with SSO" button."""
    if settings.oauth_login_provider != "oidc":
        raise HTTPException(status_code=404, detail={"error": "sso_not_configured"})
    if not _sso_starts_by_address.allow(client_address(request)):
        raise HTTPException(status_code=429, headers={"Retry-After": "900"})
    return await start_oidc_login("dashboard")


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


@router.get("/oauth/callback")
async def oidc_callback(request: Request) -> Response:
    response = await _finish_oidc_login(request)
    state = request.query_params.get("state", "")
    if state:
        response.delete_cookie(_state_cookie(state)[0], path="/oauth/callback")
    return response


async def _finish_oidc_login(request: Request) -> Response:
    from server.auth import device, oidc
    from server.auth.login import jit_provision_for_email, resolve_identity_for_email

    state = request.query_params.get("state", "")
    if not state:
        return _expired_sign_in()
    name, value = _state_cookie(state)
    if not secrets.compare_digest(request.cookies.get(name, ""), value):
        return _expired_sign_in()
    pending = await store.take_pending(state)  # single-use
    if pending is None:
        return _expired_sign_in()
    kind = pending["kind"]
    user_code = device.unseal_user_code(pending["user_code"]) if kind == "device" else ""
    if request.query_params.get("error"):
        # Cancelled at the IdP, or refused there. A device grant stays pending:
        # who cancelled is unknown, so it is no reason to end anyone's sign-in.
        await _signin_denied("", "cancelled at the identity provider")
        return _denied_page("The identity provider did not sign you in; try again")
    code = request.query_params.get("code", "")
    if not code:
        raise HTTPException(status_code=400, detail={"error": "invalid_request"})
    try:
        if not await oidc.check_callback_issuer(request.query_params.get("iss", "")):
            raise HTTPException(status_code=400, detail={"error": "invalid_issuer"})
        tokens = await oidc.exchange_code(
            code=code,
            redirect_uri=_oidc_callback_url(),
            code_verifier=pending["code_verifier"],
        )
        claims = await oidc.verify_id_token(tokens.get("id_token", ""), nonce=pending["nonce"])
    except HTTPException:
        raise
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        # The IdP is unreachable or refused the exchange: not the user's fault,
        # and its response body stays in the log, not the browser.
        log.warning("oidc callback: identity provider exchange failed: %s", type(e).__name__)
        raise HTTPException(status_code=502, detail={"error": "temporarily_unavailable"}) from e
    except Exception as e:  # any other verify failure refuses the login (re-raised)
        log.warning("oidc callback: id_token refused: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail={"error": "oidc_error"}) from e
    email = oidc.verified_email(claims)
    resolved = None
    if email:
        # No account — auto-provision one if JIT is on (else stay fail-closed).
        resolved = await resolve_identity_for_email(email) or await jit_provision_for_email(email)
    upstream = str(tokens.get("refresh_token") or "")
    if resolved is None:
        reason = "no loom account for this email" if email else "verified email required"
        await _signin_denied(str(claims.get("email") or ""), reason)
        if upstream:
            await oidc.revoke(upstream)
        if kind == "device":
            await device.deny(user_code)
        return _denied_page(f"{reason}; ask your Loom operator for access")
    if kind == "device":
        return await device.signed_in(user_code, resolved, upstream)
    if kind == "dashboard":
        return await _dashboard_signed_in(resolved)
    return await _connect_consent(pending, resolved, upstream)


async def _connect_consent(
    pending: dict[str, Any],
    identity: tuple[str, str, list[str], list[str]],
    upstream_refresh: str,
) -> Response:
    """Under OIDC the IdP may sign the user in without a single click, so the
    MCP client gets its code only after the user approves here, on a page that
    names the client and where the authorization goes."""
    client = await store.get_client(pending["client_id"])
    if client is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_client"})
    ticket = secrets.token_urlsafe(32)
    await store.save_pending(
        token_id(ticket),
        {
            "kind": "connect",
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "code_challenge": pending["code_challenge"],
            "mcp_state": pending["mcp_state"],
            "identity": list(identity),
            "upstream_refresh": seal(_UPSTREAM_PURPOSE, upstream_refresh),
        },
    )
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
    pending = await store.take_pending(token_id(f.get("ticket", "")))
    if pending is None or pending.get("kind") != "connect":
        return html_page("Connect Loom", "<h2>This approval has expired</h2>", 400)
    subject, org, namespaces, roles = pending["identity"]
    approved = f.get("decision") == "approve"
    await audit.emit(
        "auth.connect_approved" if approved else "auth.connect_denied",
        org=org,
        namespace="-",
        actor=subject,
        subject={"client_id": pending["client_id"], "redirect_uri": pending["redirect_uri"]},
    )
    client = {
        "client_id": pending["client_id"],
        "redirect_uri": pending["redirect_uri"],
        "code_challenge": pending["code_challenge"],
    }
    if not approved:
        upstream = unseal(_UPSTREAM_PURPOSE, pending["upstream_refresh"])
        if upstream:
            from server.auth import oidc

            await oidc.revoke(upstream)
        return _back_to_client(
            client["redirect_uri"], {"error": "access_denied"}, pending["mcp_state"]
        )
    return await _redirect_with_code(
        client,
        (subject, org, namespaces, roles),
        upstream_refresh=unseal(_UPSTREAM_PURPOSE, pending["upstream_refresh"]),
        state=pending["mcp_state"],
    )


async def _dashboard_signed_in(identity: tuple[str, str, list[str], list[str]]) -> Response:
    """Hand the dashboard its token the way a sign-in link does: in the URL
    fragment, which the browser never sends to a server. The token is short
    because the dashboard cannot refresh it and only a refresh asks the IdP
    whether the user is still active."""
    subject, org, namespaces, roles = identity
    family = new_jti()
    token = mint_token(
        subject=subject,
        org=org,
        namespaces=namespaces,
        roles=roles,
        ttl_seconds=_ACCESS_TTL,
        is_service_account=False,
        # A sign-in session like the CLI's: revocable as one, and what it mints
        # is bounded like the person, not by its hour.
        session_id=family,
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
def _invalid_grant(description: str = "") -> HTTPException:
    detail = {"error": "invalid_grant"}
    if description:
        detail["error_description"] = description
    return HTTPException(status_code=400, detail=detail)


async def issue_tokens(grant: dict[str, Any], client_id: str) -> dict[str, Any]:
    """An access token for the grant's session, and the next refresh token.
    Under OIDC a session with no IdP refresh token gets no Loom refresh token:
    nothing could tell Loom the user was deactivated, so the session ends with
    its access token. A subject revoked outright gets nothing, however it
    signed in."""
    from server.auth.revocation import is_revoked

    if is_revoked(grant["subject"], issued_at=int(time.time())):
        raise _invalid_grant("this account's tokens are revoked")
    access = mint_token(
        subject=grant["subject"],
        org=grant["org"],
        namespaces=list(grant["namespaces"]),
        roles=list(grant["roles"]),
        ttl_seconds=_ACCESS_TTL,
        is_service_account=False,
        session_id=grant["family"] or None,
    )
    body = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": _ACCESS_TTL,
        "scope": "loom",
    }
    if settings.oauth_login_provider == "oidc" and not grant["upstream_refresh"]:
        return body
    refresh = secrets.token_urlsafe(32)
    await store.save_refresh(
        refresh,
        {
            "client_id": client_id,
            "subject": grant["subject"],
            "org": grant["org"],
            "namespaces": list(grant["namespaces"]),
            "roles": list(grant["roles"]),
            "exp": int(time.time()) + _REFRESH_TTL,
            "family": grant["family"],
            "upstream_refresh": grant["upstream_refresh"],
        },
    )
    body["refresh_token"] = refresh
    return body


async def end_session(grant: dict[str, Any], reason: str, *, stolen: bool = False) -> None:
    """Revoke the grant's session family: its refresh tokens stop refreshing and
    its access tokens stop verifying. `stolen` (a spent credential came back,
    so someone else holds a copy) also revokes the machine tokens minted from
    this session (delegation.minted_by); a logout leaves
    those to their owner. Raises when a revocation did not land."""
    from server.auth.delegation import minted_by
    from server.auth.revocation import revoke

    if not grant["family"]:
        return  # nothing was issued under it (a denied grant), or it predates families
    until = datetime.now(UTC) + timedelta(seconds=_REFRESH_TTL)
    await revoke(grant["family"], expires_at=until, reason=reason, revoked_by="oauth")
    if stolen:
        await revoke(minted_by(grant["family"]), expires_at=None, reason=reason, revoked_by="oauth")
    await audit.emit(
        "auth.session_revoked",
        org=grant["org"],
        namespace="-",
        actor=grant["subject"],
        subject={
            "family": grant["family"],
            "client_id": grant["client_id"],
            "reason": reason,
            "machine_tokens": stolen,
        },
    )


# A spent credential presented again this soon after is a client retrying a
# request whose answer it lost, not a thief.
_RETRY_GRACE_S = 30


async def end_replayed_session(e: CodeReplayed, what: str) -> HTTPException:
    """A spent credential came back. Its session ends either way; unless it is
    a quick retry, someone else holds a copy, and what the session minted
    ends too."""
    stolen = time.time() - e.spent_at > _RETRY_GRACE_S
    try:
        await end_session(e.grant, f"{what} replayed", stolen=stolen)
    except Exception:  # the replay is refused whether or not the revoke landed
        log.exception("oauth: revoking a replayed %s's session failed", what)
    return _invalid_grant()


async def _authorization_code_grant(f: dict[str, str]) -> dict[str, Any]:
    client_id = f.get("client_id", "")
    try:
        grant = await store.take_code(f.get("code", ""), client_id)  # single-use
    except CodeReplayed as e:
        if not verify_pkce_s256(f.get("code_verifier", ""), e.grant["code_challenge"]):
            raise _invalid_grant() from None  # a leaked code alone proves nothing
        raise await end_replayed_session(e, "authorization code") from None
    if grant is None:
        raise _invalid_grant()
    if grant["redirect_uri"] != f.get("redirect_uri", ""):
        raise _invalid_grant()
    if not verify_pkce_s256(f.get("code_verifier", ""), grant["code_challenge"]):
        raise _invalid_grant("PKCE failed")
    return await issue_tokens(grant, client_id)


async def _ask_identity_provider(grant: dict[str, Any], refresh_token: str) -> str:
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
        await end_session(grant, "identity provider refused the user")
        raise _invalid_grant("the identity provider ended this session") from None
    except oidc.UpstreamUnavailable as e:
        # Not the user's fault: put the spent token back so the retry works.
        log.warning("oauth refresh: identity provider unavailable: %s", e)
        await store.save_refresh(refresh_token, grant)
        raise HTTPException(
            status_code=503,
            detail={"error": "temporarily_unavailable"},
            headers={"Retry-After": "5"},
        ) from e


async def _refresh_token_grant(f: dict[str, str]) -> dict[str, Any]:
    from server.auth.revocation import loaded

    # A refresh mints a token with a fresh iat, which would pass any
    # `revoke-token --issued-before` cutoff — so the revocation check must
    # run here, against the refresh token's own issue time. Before the
    # first snapshot load that check can't be answered: refuse without
    # consuming the token so the client can retry.
    if not loaded():
        raise HTTPException(
            status_code=503,
            detail={"error": "temporarily_unavailable"},
            headers={"Retry-After": "5"},
        )
    # A public client identifies itself on refresh (RFC 6749 §6), and the
    # token refreshes only for the client it was issued to; a mismatch is
    # refused unconsumed.
    client_id = f.get("client_id", "")
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "client_id required"},
        )
    refresh_token = f.get("refresh_token", "")
    try:
        grant = await store.take_refresh(refresh_token, client_id)  # rotates
    except CodeReplayed as e:
        raise await end_replayed_session(e, "refresh token") from None
    if grant is None:
        raise _invalid_grant()
    try:
        return await _refresh_session(grant, refresh_token)
    except HTTPException:
        raise  # an answer: refused, or retry later with the token put back
    except Exception:  # Loom's own failure: unspend the token so the retry is no replay
        await store.save_refresh(refresh_token, grant)
        raise


async def _refresh_session(grant: dict[str, Any], refresh_token: str) -> dict[str, Any]:
    """The refresh grant's checks and new tokens, once the old token is spent."""
    from server.auth.login import refreshed_roles
    from server.auth.revocation import is_revoked

    # exp is always saved as mint time + _REFRESH_TTL.
    issued_at = int(grant["exp"]) - _REFRESH_TTL
    if is_revoked(grant["family"]) or is_revoked(str(grant["subject"]), issued_at=issued_at):
        raise _invalid_grant()
    if settings.oauth_login_provider == "oidc":
        grant["upstream_refresh"] = await _ask_identity_provider(grant, refresh_token)
    # The grant is a sign-in snapshot; each refresh re-checks the account
    # is still active and re-derives its roles, so neither a released
    # account nor a removed host rides a rolling 30-day chain.
    roles = await refreshed_roles(grant["subject"], grant["org"], list(grant["roles"]))
    if roles is None:
        raise _invalid_grant()
    return await issue_tokens({**grant, "roles": roles}, grant["client_id"])


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    from server.auth import device

    f = await form_body(request)
    grant_type = f.get("grant_type", "")
    if grant_type == "authorization_code":
        return JSONResponse(await _authorization_code_grant(f))
    if grant_type == "refresh_token":
        return JSONResponse(await _refresh_token_grant(f))
    if grant_type == device.DEVICE_GRANT:
        return JSONResponse(await device.device_code_grant(f))
    raise HTTPException(status_code=400, detail={"error": "unsupported_grant_type"})


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
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "client_id required"},
        )
    grant = None
    try:
        grant = await store.take_refresh(token_value, client_id)
    except CodeReplayed as e:
        grant = e.grant
    if grant is not None:
        await end_session(grant, "logout")
        if grant["upstream_refresh"] and settings.oauth_login_provider == "oidc":
            from server.auth import oidc

            await oidc.revoke(grant["upstream_refresh"])
        return Response(status_code=200)
    try:
        info = verify_token(token_value)
    except TokenError:
        return Response(status_code=200)  # unknown, expired or already revoked
    # A session's access token ends its whole session; any other token itself.
    key = info.session_id or info.token_id
    if key:
        await revoke(
            key, expires_at=_revocation_end(info), reason="logout", revoked_by=info.subject
        )
        await audit.emit(
            "auth.session_revoked",
            org=info.org,
            namespace="-",
            actor=info.subject,
            subject={"family": info.session_id, "jti": info.token_id, "reason": "logout"},
        )
    return Response(status_code=200)


def attach(app: Any) -> None:
    """Mount sign-in wherever there is something to sign in to, and MCP
    connect (dynamic client registration, an opt-in surface) only when enabled."""
    from server.auth import device, oidc

    if settings.auth_actually_required() or settings.oauth_enabled:
        oidc.validate_config()  # fail fast if provider=oidc but IdP config missing
        app.include_router(router)
        app.include_router(device.router)
    if settings.oauth_enabled:
        app.include_router(connect_router)
