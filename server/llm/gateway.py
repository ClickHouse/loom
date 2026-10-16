"""Loom's own model calls through an inference gateway, with no provider key.

With `LOOM_INFERENCE_TOKEN_URL` set, Loom proves itself to the gateway with
its workload's OIDC token (a projected service-account token, rotated in a
file by its platform) and exchanges it there for an access token (RFC 8693),
which every chat and embedding call then carries as its bearer. The gateway
must trust the workload's issuer. Unset, the provider's key is the bearer, as
always; `LOOM_OPENAI_BASE_URL` / `LOOM_ANTHROPIC_BASE_URL` point either at a
gateway.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx

from server.settings import settings

log = logging.getLogger(__name__)

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
# Renewed halfway through its life or this long before it runs out, whichever
# is sooner; on a failed renewal the token in hand serves until it runs out,
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


async def bearer(provider_key: str) -> str:
    """What a model call presents: the gateway's token when Loom exchanges
    its workload's, else the provider key."""
    if not exchanging():
        return provider_key
    if time.time() < _renew_at and _token:
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
        workload = await asyncio.to_thread(Path(settings.inference_workload_token_file).read_text)
        form = {
            "grant_type": TOKEN_EXCHANGE,
            "subject_token": workload.strip(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
        }
        if settings.inference_token_scope:
            form["scope"] = settings.inference_token_scope
        r = await http().post(settings.inference_token_url, data=form, timeout=10.0)
        body = r.json() if r.content else {}
        if r.status_code != 200:
            raise ValueError(f"{r.status_code} {body.get('error', '')}".strip())
        token, life = str(body["access_token"]), float(body.get("expires_in", 900))
        if life <= 0:
            raise ValueError(f"expires_in {life}")
    except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as e:
        # The gateway's status and error code, never a token.
        log.warning("inference gateway token exchange failed: %s", e)
        _renew_at = time.time() + _RETRY_S  # the token in hand serves until it runs out
        return
    now = time.time()
    _token, _expires_at = token, now + life
    _renew_at = _expires_at - min(_RENEW_BEFORE_S, life / 2)


def _reset_for_tests() -> None:
    global _token, _expires_at, _renew_at
    _token, _expires_at, _renew_at = "", 0.0, 0.0
