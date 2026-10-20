"""The keys of the end-user issuers that publish them at a `jwks_url`
(`jwt_end_user_issuers_json`), fetched in the background so a token check reads
memory only.

Each process fetches every such issuer's JWKS at boot and then every
`jwt_end_user_jwks_refresh_seconds`; a fetch that fails keeps the last keys it
had. A key the issuer rotates in is honoured from the next fetch, so an issuer
publishes a new key before it signs with it. Until an issuer's first fetch lands,
its tokens are answered 503 (retry), never admitted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import jwt

from server.settings import settings

log = logging.getLogger(__name__)

# issuer -> {kid: public key}, from the last fetch that succeeded.
_keys: dict[str, dict[str, Any]] = {}
_warned: set[str] = set()


def loaded(issuer: str) -> bool:
    """Whether a fetch of `issuer`'s JWKS has landed in this process."""
    return issuer in _keys


def key(issuer: str, kid: str) -> Any | None:
    """`issuer`'s public key of `kid` from its last fetched JWKS, or None."""
    return _keys.get(issuer, {}).get(kid)


async def refresh() -> None:
    """Fetch the JWKS of every end-user issuer that names a `jwks_url`, side by
    side: an unreachable one delays boot by one timeout, not one each."""
    issuers = settings.end_user_issuers()
    await asyncio.gather(
        *(_fetch(issuer, spec.jwks_url) for issuer, spec in issuers.items() if spec.jwks_url)
    )


async def _fetch(issuer: str, url: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(response.json()).keys
        _keys[issuer] = {k.key_id: k.key for k in keys if k.key_id}
    except Exception as e:  # noqa: BLE001 — a failed fetch keeps the last keys
        if issuer not in _warned:
            _warned.add(issuer)
            log.warning("end-user issuer %r: JWKS not fetched: %s", issuer, e)
        return
    _warned.discard(issuer)


def _all_loaded() -> bool:
    return all(loaded(i) for i, spec in settings.end_user_issuers().items() if spec.jwks_url)


def start_refresh_loop() -> asyncio.Task[None]:
    """Re-fetch every `jwt_end_user_jwks_refresh_seconds` until cancelled."""

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("auth.end_user_jwks_refresh")
        while True:
            # Until every issuer's first fetch lands, retry within seconds: its
            # tokens are answered 503 until then.
            period = settings.jwt_end_user_jwks_refresh_seconds if _all_loaded() else 2.0
            await asyncio.sleep(period)
            await refresh()

    return asyncio.get_running_loop().create_task(_run(), name="loom-end-user-jwks")


def clear() -> None:
    """Test seam: forget every fetched key."""
    _keys.clear()
    _warned.clear()
