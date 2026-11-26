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

_COLLECTOR_URL = "https://loom.clickhouse.com/api/v1/adoption"
_WINDOW = timedelta(hours=12)
# not a setting: bounds reads of the collector acknowledgement record.
_MAX_ACK_BYTES = 4096


async def run_pass() -> None:
    if not settings.adoption_telemetry_enabled:
        return

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

    restricted = (
        configured_restricted_pairs() if settings.adoption_telemetry_share_domains else None
    )
    report = await window_report(start, end, installation_id, restricted=restricted, ch=ch)
    pass_context.check_stop()
    try:
        async with get_http_client().stream(
            "POST",
            _COLLECTOR_URL,
            json=report,
            follow_redirects=False,
            timeout=8.0,  # not a setting: allows the collector's six-second insert budget.
        ) as response:
            await _require_acknowledgement(response, str(installation_id), cursor)
            pass_context.set_cursor(cursor)
    except httpx.RequestError as exc:
        pass_context.note_failure(RuntimeError(type(exc).__name__), "adoption report")
    except ValueError as exc:
        pass_context.note_failure(exc, "adoption report")


async def _require_acknowledgement(
    response: httpx.Response, installation_id: str, window_start: str
) -> None:
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
    if (
        not isinstance(result, dict)
        or result.get("stored") is not True
        or result.get("installation_id") != installation_id
        or result.get("window_start") != window_start
    ):
        raise ValueError("collector returned an invalid acknowledgement")
