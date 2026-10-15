"""OIDC client for brokered login.

When `settings.oauth_login_provider == "oidc"`, loom delegates the human
sign-in at `/oauth/authorize` to an external identity provider (Okta, Auth0,
Google, Azure AD, Keycloak, …) instead of its own password form. loom stays
the authorization server the MCP client talks to — only the human-login step
redirects to the IdP. loom is a confidential client (client_secret) and sends
PKCE too; `state` guards CSRF and `nonce` guards id_token replay. The verified
email is mapped to a loom account by `server.auth.login.resolve_identity_for_email`.

When the IdP advertises `offline_access`, loom asks for it and keeps the IdP's
refresh token (sealed). Every Loom refresh spends it at the IdP first, so a
user the IdP has deactivated loses Loom at their next refresh.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from server.settings import settings

log = logging.getLogger(__name__)

_DISCOVERY_TTL = 3600.0
# issuer -> (expiry_epoch, openid-configuration document)
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwk_clients: dict[str, PyJWKClient] = {}


async def _discover() -> dict[str, Any]:
    """Fetch + cache the IdP's OpenID configuration (endpoints + jwks_uri).

    The document must name the configured issuer (OpenID Discovery §4.3):
    its `issuer` is what id_tokens are checked against, and its endpoints are
    where loom sends the client secret and fetches signing keys.
    """
    issuer = settings.oidc_issuer.rstrip("/")
    cached = _discovery_cache.get(issuer)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{issuer}/.well-known/openid-configuration")
        r.raise_for_status()
        doc: dict[str, Any] = r.json()
    if str(doc.get("issuer") or "").rstrip("/") != issuer:
        raise ValueError(f"discovery issuer {doc.get('issuer')!r} is not {issuer!r}")
    _discovery_cache[issuer] = (now + _DISCOVERY_TTL, doc)
    return doc


# The whole IdP leg of a Loom refresh (discovery and the token call): the
# SDK waits longer than this for a refresh, so it never gives up on one the
# server is still answering.
_REFRESH_DEADLINE_S = 20


class UpstreamRefused(Exception):
    """The IdP answered a refresh with `invalid_grant`: the user's IdP session
    is gone (deactivated, signed out everywhere, token revoked)."""


class UpstreamUnavailable(Exception):
    """The IdP could not answer a refresh (down, a 5xx, a rejected client
    secret, a malformed reply): Loom's or the IdP's problem, not the user's."""


async def build_authorize_url(
    *, redirect_uri: str, state: str, nonce: str, code_challenge: str, offline: bool
) -> str:
    """The IdP authorize URL loom redirects the browser to (auth-code flow).
    `offline` asks for an IdP refresh token, for a sign-in whose Loom session
    refreshes; some IdPs reject a scope they don't list, so only where the
    discovery document advertises `offline_access`."""
    doc = await _discover()
    scope = "openid email profile"
    if offline and "offline_access" in (doc.get("scopes_supported") or []):
        scope += " offline_access"
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{doc['authorization_endpoint']}?{urlencode(params)}"


async def _token_request(form: dict[str, str]) -> dict[str, Any]:
    doc = await _discover()
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(
            doc["token_endpoint"],
            data={
                **form,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
            },
        )
    r.raise_for_status()
    tokens = r.json()
    # A success is an object naming the token it issued (RFC 6749 §5.1).
    if (
        not isinstance(tokens, dict)
        or not tokens.get("access_token")
        or not tokens.get("token_type")
    ):
        raise ValueError("the identity provider's token response is not a token response")
    return tokens


async def exchange_code(*, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
    """Exchange the IdP authorization code for tokens (confidential client)."""
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    )


async def refresh(refresh_token: str) -> str:
    """Spend the IdP refresh token; returns the one to keep next (the IdP may
    rotate it). Raises UpstreamRefused for `invalid_grant` and
    UpstreamUnavailable for anything else that goes wrong."""
    try:
        async with asyncio.timeout(_REFRESH_DEADLINE_S):
            tokens = await _token_request(
                {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400 and error_code(e.response) == "invalid_grant":
            raise UpstreamRefused from e
        raise UpstreamUnavailable(f"HTTP {e.response.status_code}") from e
    except (httpx.HTTPError, ValueError, KeyError, TimeoutError) as e:
        raise UpstreamUnavailable(type(e).__name__) from e
    return str(tokens.get("refresh_token") or refresh_token)


def error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error") or "") if isinstance(body, dict) else ""


async def revoke(refresh_token: str) -> None:
    """Revoke the IdP refresh token at the IdP's revocation endpoint (RFC 7009)
    when it has one. Best effort: logout has already ended the Loom session."""
    if not refresh_token:
        return
    try:
        endpoint = (await _discover()).get("revocation_endpoint")
        if not endpoint:
            return
        async with httpx.AsyncClient(timeout=10.0) as c:
            await c.post(
                endpoint,
                data={
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                    "client_id": settings.oidc_client_id,
                    "client_secret": settings.oidc_client_secret,
                },
            )
    except (httpx.HTTPError, ValueError) as e:
        log.warning("oidc: revoking the identity provider's token failed: %s", type(e).__name__)


async def check_callback_issuer(iss: str) -> bool:
    """RFC 9207: an IdP that names itself on the callback must be the one loom
    sent the user to. Absent is fine (not every IdP sends it)."""
    if not iss:
        return True
    doc = await _discover()
    return iss.rstrip("/") == str(doc["issuer"]).rstrip("/")


def _signing_key(jwks_uri: str, id_token: str) -> Any:
    """Resolve the IdP signing key for this token. Sync (PyJWKClient's fetch);
    callers run it off the event loop. One client per jwks_uri keeps its key
    cache, and an unknown `kid` refetches once, which follows IdP rotation."""
    client = _jwk_clients.get(jwks_uri)
    if client is None:
        client = _jwk_clients[jwks_uri] = PyJWKClient(jwks_uri, timeout=10)
    return client.get_signing_key_from_jwt(id_token).key


async def verify_id_token(id_token: str, *, nonce: str) -> dict[str, Any]:
    """Verify the IdP id_token (signature against JWKS, iss, aud, exp) and the
    nonce, returning the claims. Raises on any failure."""
    doc = await _discover()
    key = await asyncio.to_thread(_signing_key, doc["jwks_uri"], id_token)
    claims: dict[str, Any] = jwt.decode(
        id_token,
        key,
        algorithms=["RS256"],
        audience=settings.oidc_client_id,
        issuer=doc["issuer"],
        options={"require": ["exp", "iat", "aud", "iss"]},
    )
    if not nonce or not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
        raise ValueError("id_token nonce mismatch")
    return claims


def verified_email(claims: dict[str, Any]) -> str:
    """The id_token's email when the IdP asserts it verified it, else ''.
    `email_verified` must be the boolean true (or the string "true" some IdPs
    send): a truthiness test would take the string "false" as verified."""
    flag = claims.get("email_verified")
    if flag is True or (isinstance(flag, str) and flag.strip().lower() == "true"):
        return str(claims.get("email") or "").strip()
    return ""


def validate_config() -> None:
    """Fail fast at boot when provider=oidc but the IdP config is incomplete."""
    if settings.oauth_login_provider != "oidc":
        return
    missing = [
        name
        for name, value in {
            # Required: loom's own stable public base URL. The IdP callback /
            # redirect_uri MUST come from trusted config, not the request Host
            # header — otherwise a spoofed Host could send an attacker-domain
            # callback to the IdP.
            "LOOM_OAUTH_ISSUER": settings.oauth_issuer,
            "LOOM_OIDC_ISSUER": settings.oidc_issuer,
            "LOOM_OIDC_CLIENT_ID": settings.oidc_client_id,
            "LOOM_OIDC_CLIENT_SECRET": settings.oidc_client_secret,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"oauth_login_provider='oidc' requires {', '.join(missing)}")
    # JIT provisioning needs a target org to create accounts in.
    if settings.oidc_jit_provision and not settings.oidc_jit_org:
        raise RuntimeError("oidc_jit_provision=true requires LOOM_OIDC_JIT_ORG")
