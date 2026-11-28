from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from server.insights.workers import adoption_telemetry
from server.settings import settings
from server.workers import pass_context

_COLLECTOR_LIMIT = 1_048_576


@pytest.mark.parametrize(
    ("domain_count", "details_fit", "names_fit"),
    [(2, True, True), (1800, False, True), (4000, False, False)],
    ids=["full", "names-only", "limited-names"],
)
async def test_sender_preserves_aggregates_within_the_collector_limit(
    monkeypatch: pytest.MonkeyPatch, domain_count: int, details_fit: bool, names_fit: bool
) -> None:
    domains = [
        {
            "domain": f"{index:04}{'x' * 59}.{'y' * 63}.{'z' * 63}.{'w' * 61}",
            "user_count": 1,
            "successful_requests": index + 1,
        }
        for index in range(domain_count)
    ]
    report = {
        "schema_version": 3,
        "successful_requests": 0,
        "daily_successful_requests": sum(row["successful_requests"] for row in domains),
        "active_accounts": 1,
        "serving_processes": 1,
        "user_domains": domains,
        "domain_activity": [
            {
                "domain": row["domain"],
                "successful_requests": row["successful_requests"],
                "active_accounts": 1,
                "serving_processes": 1,
            }
            for row in domains
        ],
    }
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(
        adoption_telemetry, "utc_now", lambda: datetime(2026, 11, 24, 3, tzinfo=UTC)
    )
    monkeypatch.setattr(adoption_telemetry, "get_client", object)
    monkeypatch.setattr(
        adoption_telemetry,
        "recorded_authority",
        AsyncMock(return_value={"deployment_id": str(uuid4())}),
    )

    async def aggregate(
        start: datetime, end: datetime, installation_id: UUID, **_: Any
    ) -> dict[str, Any]:
        return {
            **report,
            "installation_id": str(installation_id),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        }

    monkeypatch.setattr(adoption_telemetry, "window_report", aggregate)
    monkeypatch.setattr(adoption_telemetry, "domain_snapshot", AsyncMock(return_value=None))
    requests: list[httpx.Request] = []

    def collect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(request.content) > _COLLECTOR_LIMIT:
            return httpx.Response(413)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "stored": True,
                "installation_id": payload["installation_id"],
                "window_start": payload["window_start"],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(collect)) as client:
        monkeypatch.setattr(adoption_telemetry, "get_http_client", lambda: client)
        with pass_context.ensure_pass("adoption_telemetry") as ctx:
            await adoption_telemetry.run_pass()
            assert ctx.cursor == "2026-11-23T12:00:00+00:00"

    [request] = requests
    assert request.headers["Content-Type"] == "application/json"
    payload = json.loads(request.content)
    assert all(
        payload[key] == value
        for key, value in report.items()
        if key not in {"user_domains", "domain_activity"}
    )
    assert payload["domain_activity"] == (report["domain_activity"] if details_fit else [])
    if names_fit:
        assert sorted(payload["user_domains"], key=lambda row: row["domain"]) == domains
    else:
        retained = len(payload["user_domains"])
        ranked = list(reversed(domains))
        assert 0 < retained < len(domains)
        assert payload["user_domains"] == ranked[:retained]
        next_payload = {**payload, "user_domains": ranked[: retained + 1]}
        assert len(json.dumps(next_payload, separators=(",", ":")).encode()) > _COLLECTOR_LIMIT
