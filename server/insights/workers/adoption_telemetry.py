"""Send a completed 12-hour adoption report to the event collector."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from server.clients._http import get_client as get_http_client
from server.core.ch import get_client
from server.core.datetimes import utc_now
from server.core.pg_profile import recorded_authority
from server.identity.app.directory import configured_restricted_pairs
from server.insights.store.adoption_telemetry import window_report
from server.settings import settings
from server.workers import pass_context

_CAPTURE_URL = "https://eu.i.posthog.com/i/v0/e/"
_WINDOW = timedelta(hours=12)
_MAX_ACK_BYTES = 4096


async def run_pass() -> None:
    key = settings.adoption_telemetry_capture_key
    if not settings.adoption_telemetry_enabled or not key:
        return
    if not key.startswith("phc_"):
        raise ValueError("adoption telemetry needs a public event capture key")

    ch = get_client()
    authority = await recorded_authority(ch)
    if authority is None:
        pass_context.record_outcome("adoption_telemetry", {"status": "missing_deployment_id"})
        return
    # Use a separate identifier from the storage authority used for routing.
    installation_id = uuid5(NAMESPACE_URL, f"loom/adoption/v1/{UUID(authority['deployment_id'])}")
    pass_context.record_outcome("adoption_telemetry", {"installation_id": str(installation_id)})

    # Give the ledger time to flush before reading the previous window.
    cutoff = utc_now() - timedelta(hours=1)
    end = cutoff.replace(hour=cutoff.hour // 12 * 12, minute=0, second=0, microsecond=0)
    start = end - _WINDOW
    cursor = start.isoformat()
    if pass_context.resume_cursor() >= cursor:
        return

    report = await window_report(
        start, end, installation_id, restricted=configured_restricted_pairs(), ch=ch
    )
    pass_context.check_stop()
    try:
        async with get_http_client().stream(
            "POST",
            _CAPTURE_URL,
            json={
                "api_key": key,
                "event": "loom_self_hosted_telemetry",
                "distinct_id": str(installation_id),
                "timestamp": cursor,
                "uuid": str(uuid5(installation_id, cursor)),
                "properties": {
                    **report,
                    "$process_person_profile": False,
                    "$geoip_disable": True,
                },
            },
            follow_redirects=False,
            timeout=5.0,
        ) as response:
            await _require_acknowledgement(response)
            pass_context.set_cursor(cursor)
    except httpx.RequestError as exc:
        pass_context.note_failure(RuntimeError(type(exc).__name__), "adoption report")
    except ValueError as exc:
        pass_context.note_failure(exc, "adoption report")


async def _require_acknowledgement(response: httpx.Response) -> None:
    if not response.is_success:
        raise ValueError(f"collector returned HTTP {response.status_code}")
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_MAX_ACK_BYTES):
        if len(body) + len(chunk) > _MAX_ACK_BYTES:
            raise ValueError("collector acknowledgement was too large")
        body.extend(chunk)
    try:
        result = json.loads(body)
    except ValueError:
        raise ValueError("collector returned an invalid acknowledgement") from None
    if not isinstance(result, dict) or result.get("status") not in (1, "Ok"):
        raise ValueError("collector returned an invalid acknowledgement")
    if result.get("quota_limited"):
        raise ValueError("collector limited the event quota")
