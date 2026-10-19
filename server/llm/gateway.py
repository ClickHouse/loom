"""Loom's own model calls through an inference gateway, with no provider key.

With `LOOM_INFERENCE_TOKEN_URL` set, Loom proves itself to the gateway with an
identity token and exchanges it there for an access token (RFC 8693), which
every chat and embedding call then carries as its bearer. The identity token is
either the workload's, a file its platform rotates (a projected
service-account token on Kubernetes), or one Loom signs itself
(`LOOM_INFERENCE_TOKEN_AUDIENCE`): a few minutes long, from issuer
`<LOOM_PUBLIC_BASE_URL>/v1` with the newest RS256 key of the signing keyring,
which `/v1/.well-known/jwks.json` publishes. Either way the gateway must trust
the issuer. Loom's own verifier never accepts that audience, so the token opens
nothing here. Unset, the provider's key is the bearer, as always;
`LOOM_OPENAI_BASE_URL` / `LOOM_ANTHROPIC_BASE_URL` point either at a gateway.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from server.core.errors import LoomServerError
from server.settings import settings
from server.settings._providers import is_provider_api

log = logging.getLogger(__name__)

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
# How long an identity token Loom signs itself lives: it is exchanged at once.
IDENTITY_TOKEN_LIFE_S = 300  # not a setting: a longer life only widens a stolen token's use
# Its subject: the deployment, which the gateway's trust entry attributes spend to.
IDENTITY_SUBJECT = "loom"
_MAX_LIFE_S = 86400
# Renewed this long before it runs out (halfway, for a token living under ten
# minutes); on a failed renewal the token in hand serves until it runs out,
# and the next try waits this long (with no token in hand too).
_RENEW_BEFORE_S = 300
_RETRY_S = 30

_token = ""
_expires_at = 0.0
_renew_at = 0.0
_lock = asyncio.Lock()


class GatewayUnavailable(httpx.TransportError):
    """No gateway token could be had and none is in hand: an outage, retried
    and fallen back from as any transport failure is."""


def exchanging() -> bool:
    return bool(settings.inference_token_url)


async def bearer(provider_key: str, url: str) -> str:
    """What a model call to `url` presents: the gateway's token when Loom
    exchanges an identity token there, else the provider key. A call any path (a model
    named per request, the dashboard's check) sends to a provider's own API
    gets no gateway token."""
    if not exchanging():
        return provider_key
    if is_provider_api(url):
        host = urlsplit(url).hostname
        raise GatewayUnavailable(f"{host} is not the gateway: point its base URL at the gateway")
    now = time.time()
    if now < _renew_at and _token and now < _expires_at:  # a retry pause never outlasts it
        return _token
    async with _lock:
        if time.time() >= _renew_at:  # 0 at first: the first call renews
            await _renew()
    if not _token or time.time() >= _expires_at:
        raise GatewayUnavailable("the inference gateway's token could not be renewed")
    return _token


async def _renew() -> None:
    global _token, _expires_at, _renew_at
    from server.clients._http import get_client as http

    try:
        form = {
            "grant_type": TOKEN_EXCHANGE,
            "subject_token": await _identity_token(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        }
        if settings.inference_token_scope:
            form["scope"] = settings.inference_token_scope
        # Never followed: a redirect would carry the identity token elsewhere.
        r = await http().post(
            settings.inference_token_url, data=form, timeout=10.0, follow_redirects=False
        )
        if r.status_code != 200:
            raise ValueError(f"{r.status_code} {_error_code(r)}".strip())
        body = r.json()
        token, life = body["access_token"], float(body.get("expires_in", 900))
        if not isinstance(token, str) or not token:
            raise ValueError("no access_token")
        if not math.isfinite(life) or life <= 0:
            raise ValueError(f"expires_in {life}")
        life = min(life, _MAX_LIFE_S)  # kept no longer than a day, whatever it says
    except (
        OSError,
        httpx.HTTPError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        LoomServerError,
    ) as e:
        # The gateway's status and error code, never a token.
        log.warning("inference gateway token exchange failed: %s", e)
        _renew_at = time.time() + _RETRY_S  # the token in hand serves until it runs out
        return
    now = time.time()
    _token, _expires_at = token, now + life
    _renew_at = _expires_at - min(_RENEW_BEFORE_S, life / 2)


async def _identity_token() -> str:
    """What Loom exchanges: the workload's token from its file, else one it
    signs itself."""
    from server.auth import keyring

    if settings.inference_workload_token_file:
        workload = await asyncio.to_thread(Path(settings.inference_workload_token_file).read_text)
        return workload.strip()
    if not keyring.loaded() or keyring.stale():
        # A CLI command calling models (`loom reembed`) runs no refresh loop, and
        # a server's first model call can come before the loop's first read.
        await keyring.refresh()
    return signed_identity_token()


def signed_identity_token() -> str:
    """A short identity token for the exchange, signed with the newest RS256
    key. Raises ValueError when there is none (an HS256 secret is this
    deployment's alone, and the gateway could not verify it), and
    BackendUnavailableError while the keyring is not loaded or is stale."""
    import jwt as pyjwt

    from server.auth import keyring
    from server.auth.jwt import new_jti

    key = keyring.rs256_signing_key()
    if key is None:
        raise ValueError(
            "no RS256 signing key to sign the identity token: run "
            "`loom signing-key rotate --algorithm RS256` (or make LOOM_JWT_SECRET an RSA key)"
        )
    now = int(time.time())
    claims = {
        "iss": settings.inference_issuer(),
        "sub": IDENTITY_SUBJECT,
        "aud": settings.inference_token_audience,
        "iat": now,
        "nbf": now,
        "exp": now + IDENTITY_TOKEN_LIFE_S,
        "jti": new_jti(),
    }
    return pyjwt.encode(claims, key.secret, algorithm="RS256", headers={"kid": key.kid})


def _error_code(r: httpx.Response) -> str:
    """The OAuth error code of a refusal; '' for a page that is not one."""
    try:
        code = r.json().get("error", "")
    except (ValueError, AttributeError):
        return ""
    return code if isinstance(code, str) else ""


def _reset_for_tests() -> None:
    global _token, _expires_at, _renew_at
    _token, _expires_at, _renew_at = "", 0.0, 0.0
