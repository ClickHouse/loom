"""The keys of the end-user issuers that publish them at a `jwks_url`
(`jwt_end_user_issuers_json`), fetched in the background so a token check reads
memory only.

Each process fetches every such issuer's JWKS at boot and then every
`jwt_end_user_jwks_refresh_seconds`; a fetch that fails keeps the last keys it
had. A key the issuer rotates in is honoured from the next fetch, so an issuer
publishes a new key before it signs with it. Until an issuer's first fetch lands,
its tokens are answered 503 (retry), never admitted, and that issuer is also
fetched every 2 s. A fetched key that is also a federated key
(`jwt_public_keys_json`) is dropped: it could sign Loom-shaped tokens.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from server.auth.keyring import MIN_RSA_BITS
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


async def refresh(*, unloaded_only: bool = False) -> None:
    """Fetch the JWKS of every end-user issuer that names a `jwks_url` (or of
    those not loaded yet), side by side: an unreachable one delays boot by one
    timeout, not one each."""
    issuers = settings.end_user_issuers()
    await asyncio.gather(
        *(
            _fetch(issuer, spec.jwks_url)
            for issuer, spec in issuers.items()
            if spec.jwks_url and not (unloaded_only and loaded(issuer))
        )
    )


async def _fetch(issuer: str, url: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=settings.identity_provider_timeout_s) as client:
            response = await client.get(url)
            response.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(response.json()).keys
        federated = settings.federated_key_numbers()
        found = {k.key_id: k.key for k in keys if k.key_id}
        shared = sorted(kid for kid, k in found.items() if _numbers(k) in federated)
        if shared:
            log.warning("end-user issuer %r: keys %s are federated keys, dropped", issuer, shared)
        # An RSA key under RFC 7518's minimum: its published modulus can be factored.
        weak = sorted(
            kid
            for kid, k in found.items()
            if isinstance(k, (rsa.RSAPublicKey, rsa.RSAPrivateKey)) and k.key_size < MIN_RSA_BITS
        )
        if weak:
            log.warning("end-user issuer %r: keys %s are under 2048 bits, dropped", issuer, weak)
        _keys[issuer] = {
            kid: k for kid, k in found.items() if kid not in shared and kid not in weak
        }
    except Exception as e:  # noqa: BLE001 — a failed fetch keeps the last keys
        if issuer not in _warned:
            _warned.add(issuer)
            log.warning("end-user issuer %r: JWKS not fetched: %s", issuer, e)
        return
    _warned.discard(issuer)


def _numbers(key: Any) -> Any:
    try:
        return key.public_numbers()
    except Exception:  # noqa: BLE001 — a key with no numbers matches none
        return None


# Test seam: the loop's clock.
_now = time.monotonic


def _all_loaded() -> bool:
    return all(loaded(i) for i, spec in settings.end_user_issuers().items() if spec.jwks_url)


def start_refresh_loop() -> asyncio.Task[None]:
    """Re-fetch every `jwt_end_user_jwks_refresh_seconds` until cancelled;
    while an issuer's first fetch has not landed, that issuer every 2 s as
    well."""

    async def _run() -> None:
        from server.core.ch import set_query_tag

        set_query_tag("auth.end_user_jwks_refresh")
        period = settings.jwt_end_user_jwks_refresh_seconds
        next_full = _now() + period
        while True:
            if _all_loaded():
                await asyncio.sleep(period)
            else:
                # Until every issuer's first fetch lands, retry it within
                # seconds (its tokens are answered 503 until then), and keep
                # the others on their period: one dead URL never freezes the
                # keys of an issuer that rotates.
                await asyncio.sleep(2.0)
                if _now() < next_full:
                    await refresh(unloaded_only=True)
                    continue
            await refresh()
            period = settings.jwt_end_user_jwks_refresh_seconds
            next_full = _now() + period

    return asyncio.get_running_loop().create_task(_run(), name="loom-end-user-jwks")


def clear() -> None:
    """Test seam: forget every fetched key."""
    _keys.clear()
    _warned.clear()
