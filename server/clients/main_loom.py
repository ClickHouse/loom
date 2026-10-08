"""A read-replica's calls to the main Loom: the ops that read git history,
sent with the caller's own headers (`server.core.read_replica`), and the
replica's own calls (`call`), made with a service token it mints itself.

Every call and a periodic probe record whether the main Loom answered
(`MAIN_STATE`), which `/v1/health` reports, so a replica whose main Loom is
gone says so instead of looking healthy."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel

from server.clients._http import get_client
from server.clients._retry import RETRYABLE_STATUS
from server.core import read_replica
from server.core.errors import BackendUnavailableError
from server.settings import settings

log = logging.getLogger("loom.read_replica")

# The replica's service token lives this long and is re-minted this close to expiry.
_TOKEN_TTL_S = 900
_TOKEN_RENEW_S = 120
_token: tuple[str, float] | None = None

# Whether the main Loom answered this replica last time anything asked: a
# call or the probe. `reachable` None = not asked yet.
MAIN_STATE: dict[str, Any] = {"reachable": None, "checked_at": "", "error": ""}


def _note(ok: bool, error: str = "") -> None:
    MAIN_STATE.update(
        reachable=ok, checked_at=datetime.now(UTC).isoformat(timespec="seconds"), error=error
    )


def service_token() -> str:
    """This replica's own token for the main Loom: a short-lived service token
    signed with the deployment keys the replica holds, so no credential is
    copied for it. Its subject names this process."""
    global _token
    now = time.time()
    if _token is not None and _token[1] - now > _TOKEN_RENEW_S:
        return _token[0]
    from server.auth.jwt import mint_token
    from server.core.provider_process import process_identity

    token = mint_token(
        subject=f"{read_replica.REPLICA_SUBJECT_PREFIX}{process_identity()}",
        org=str(settings.code_org or settings.tracker_org or "-"),
        namespaces=[],
        roles=["viewer"],
        ttl_seconds=_TOKEN_TTL_S,
        is_service_account=True,
    )
    _token = (token, now + _TOKEN_TTL_S)
    return token


def main_url() -> str:
    return str(settings.read_replica_main_url).strip().rstrip("/")


async def _post_once(op_name: str, body: dict[str, Any], timeout_s: float) -> httpx.Response:
    return await get_client().post(
        f"{main_url()}/v1/{op_name}",
        json=body,
        headers={"Authorization": f"Bearer {service_token()}"},
        timeout=timeout_s,
    )


async def call(
    op_name: str,
    body: dict[str, Any],
    *,
    timeout_s: float | None = None,
    idempotent: bool = True,
) -> dict[str, Any]:
    """The main Loom's answer to `op_name`, called as this replica.

    A dropped connection is tried once more; a call that is `idempotent` is
    also retried on a timeout or a retryable status (a completion is not:
    the main Loom may already have answered it). A call the main Loom does
    not answer raises BackendUnavailableError (503 BACKEND_UNAVAILABLE),
    which both transports report by name; its message leaves out the URL."""
    timeout = float(timeout_s or settings.read_replica_forward_timeout_seconds)
    why = ""
    for attempt in range(2):
        try:
            resp = await _post_once(op_name, body, timeout)
        except httpx.ConnectError as e:
            why = f"cannot connect ({type(e).__name__})"
        except httpx.TransportError as e:
            why = f"no answer ({type(e).__name__})"
            if not idempotent:
                break
        else:
            if resp.status_code < 400:
                data = resp.json()
                if not isinstance(data, dict):
                    why = "an answer that is not a JSON object"
                    break
                _note(True)
                return data
            why = f"HTTP {resp.status_code}"
            if not (idempotent and resp.status_code in RETRYABLE_STATUS):
                break
        if attempt == 0:
            await asyncio.sleep(0.5)
    _note(False, f"{op_name}: {why}")
    log.warning("read_replica: the main Loom at %s gave %s %s", main_url(), op_name, why)
    raise BackendUnavailableError(
        f"this read-replica sends {op_name} to the main Loom, which gave {why}; "
        "retry, or check that LOOM_READ_REPLICA_MAIN_URL is reachable from the replica",
        operation=op_name,
    )


async def probe() -> None:
    """Ask the main Loom's `/v1/health` once and record whether it answered."""
    try:
        resp = await get_client().get(f"{main_url()}/v1/health", timeout=5.0)
    except httpx.HTTPError as e:
        _note(False, f"health: cannot connect ({type(e).__name__})")
        return
    if resp.status_code == 200:
        _note(True)
    else:
        _note(False, f"health: HTTP {resp.status_code}")


async def probe_loop() -> None:
    """Probe the main Loom at boot and every `read_replica_main_probe_seconds`."""
    while True:
        try:
            await probe()
        except Exception as e:  # noqa: BLE001 — the probe must never end the loop
            _note(False, f"health: {type(e).__name__}")
        await asyncio.sleep(float(settings.read_replica_main_probe_seconds))


async def forward[Out: BaseModel](
    op_name: str, payload: BaseModel, output_cls: type[Out]
) -> Out | None:
    """The main Loom's answer to `op_name` with `payload`, or None when this
    process does not forward it or the main Loom cannot be reached (the caller
    then answers without git history). An error answer from the main Loom is
    raised as an HTTPException with its status, detail and Retry-After, which
    both transports answer as they would the main Loom's own."""
    if not read_replica.forwards(op_name):
        return None
    url = f"{main_url()}/v1/{op_name}"
    try:
        resp = await get_client().post(
            url,
            json=payload.model_dump(mode="json", exclude_unset=True),
            headers=read_replica.caller_headers(),
            timeout=float(settings.read_replica_forward_timeout_seconds),
        )
    except httpx.HTTPError as e:
        log.warning("read_replica: %s not forwarded to the main Loom: %s", op_name, e)
        _note(False, f"{op_name}: cannot connect ({type(e).__name__})")
        return None
    if resp.status_code >= 500:
        log.warning("read_replica: the main Loom answered %s %s", op_name, resp.status_code)
        _note(False, f"{op_name}: HTTP {resp.status_code}")
        return None
    _note(True)
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        detail = body.get("detail", body) if isinstance(body, dict) else {}
        if not isinstance(detail, dict):
            detail = {"message": str(detail)}
        retry = resp.headers.get("retry-after")
        raise HTTPException(
            resp.status_code, detail=detail, headers={"Retry-After": retry} if retry else None
        )
    return output_cls.model_validate(resp.json())


__all__ = ["MAIN_STATE", "call", "forward", "main_url", "probe", "probe_loop", "service_token"]
