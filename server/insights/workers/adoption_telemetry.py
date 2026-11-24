"""Send a completed 12-hour adoption report to the event collector."""

from __future__ import annotations

from datetime import timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from server.clients._http import get_client as get_http_client
from server.core.ch import get_client
from server.core.datetimes import utc_now
from server.core.pg_profile import recorded_authority
from server.insights.store.adoption_telemetry import window_report
from server.settings import settings
from server.workers import pass_context

_CAPTURE_URL = "https://eu.i.posthog.com/i/v0/e/"
_WINDOW = timedelta(hours=12)


async def run_pass() -> None:
    key = settings.adoption_telemetry_capture_key
    if not settings.adoption_telemetry_enabled or not key:
        return
    if not key.startswith("phc_"):
        raise ValueError("adoption telemetry needs a public event capture key")

    ch = get_client()
    authority = await recorded_authority(ch)
    if authority is None:
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

    report = await window_report(start, end, installation_id, ch=ch)
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
            if response.is_success:
                pass_context.set_cursor(cursor)
            else:
                pass_context.note_failure(
                    RuntimeError(f"collector returned HTTP {response.status_code}"),
                    "adoption report",
                )
    except httpx.RequestError as exc:
        pass_context.note_failure(RuntimeError(type(exc).__name__), "adoption report")
