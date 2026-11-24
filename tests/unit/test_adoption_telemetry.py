import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest

from server.insights.workers import adoption_telemetry
from server.settings import Settings, settings
from server.workers import pass_context
from server.workers.registry import enabled_workers


async def test_explicit_opt_out_skips_adoption_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["adoption_telemetry_enabled"].default is True
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", False)
    monkeypatch.setattr(settings, "adoption_telemetry_capture_key", "phc_example")
    ch = AsyncMock()
    http = AsyncMock()
    monkeypatch.setattr(adoption_telemetry, "get_client", ch)
    monkeypatch.setattr(adoption_telemetry, "get_http_client", http)

    assert "adoption_telemetry" not in {worker.name for worker in enabled_workers()}
    await adoption_telemetry.run_pass()
    ch.assert_not_called()
    http.assert_not_called()


async def test_missing_deployment_id_surfaces_status_without_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(settings, "adoption_telemetry_capture_key", "phc_example")
    monkeypatch.setattr(adoption_telemetry, "get_client", object)
    monkeypatch.setattr(adoption_telemetry, "recorded_authority", AsyncMock(return_value=None))
    http = AsyncMock()
    monkeypatch.setattr(adoption_telemetry, "get_http_client", http)

    with pass_context.ensure_pass("adoption_telemetry") as ctx:
        await adoption_telemetry.run_pass()
        assert ctx.outcome == {"status": "missing_deployment_id"}
        assert ctx.cursor == ""
    http.assert_not_called()


@pytest.mark.parametrize(
    "rejection",
    [
        httpx.Response(302, headers={"Location": "https://other.example/adoption"}),
        httpx.Response(200, json={"status": 1, "quota_limited": ["events"]}),
    ],
    ids=["redirect", "quota_limited"],
)
async def test_delivery_retries_without_recounting_after_ack(
    monkeypatch: pytest.MonkeyPatch, rejection: httpx.Response
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(settings, "adoption_telemetry_capture_key", "phc_example")
    monkeypatch.setattr(
        adoption_telemetry, "utc_now", lambda: datetime(2026, 11, 24, 3, tzinfo=UTC)
    )
    deployment_id = uuid4()
    authority = AsyncMock(return_value={"deployment_id": str(deployment_id)})
    monkeypatch.setattr(adoption_telemetry, "recorded_authority", authority)
    monkeypatch.setattr(adoption_telemetry, "get_client", object)
    restricted_pairs = {("test", "restricted")}
    monkeypatch.setattr(adoption_telemetry, "configured_restricted_pairs", lambda: restricted_pairs)

    async def report(
        start: datetime,
        end: datetime,
        installation_id: UUID,
        *,
        restricted: set[tuple[str, str]],
        ch: object,
    ) -> dict[str, str]:
        assert installation_id != deployment_id
        assert start == datetime(2026, 11, 23, 12, tzinfo=UTC)
        assert end == datetime(2026, 11, 24, tzinfo=UTC)
        assert restricted == restricted_pairs
        return {"installation_id": str(installation_id), "window_start": start.isoformat()}

    aggregate = AsyncMock(side_effect=report)
    monkeypatch.setattr(adoption_telemetry, "window_report", aggregate)
    requests: list[httpx.Request] = []

    def collect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return rejection
        return httpx.Response(200, json={"status": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(collect)) as client:
        monkeypatch.setattr(adoption_telemetry, "get_http_client", lambda: client)
        with pass_context.ensure_pass("adoption_telemetry") as ctx:
            await adoption_telemetry.run_pass()
            assert ctx.outcome == {"installation_id": str(aggregate.await_args.args[2])}
            assert ctx.cursor == "" and ctx.count == 1
            await adoption_telemetry.run_pass()
            assert ctx.cursor == "2026-11-23T12:00:00+00:00"
            ctx.resume_cursor = ctx.cursor
            await adoption_telemetry.run_pass()

    assert len(requests) == 2 and aggregate.await_count == 2
    assert all(request.method == "POST" for request in requests)
    assert all(request.url.host == "eu.i.posthog.com" for request in requests)
    assert requests[0].content == requests[1].content
    event = json.loads(requests[0].content)
    assert event["api_key"] == "phc_example"
    assert event["event"] == "loom_self_hosted_telemetry"
    assert event["distinct_id"] == event["properties"]["installation_id"]
    assert UUID(event["uuid"]).version == 5
    assert event["timestamp"] == "2026-11-23T12:00:00+00:00"
    assert event["properties"]["$process_person_profile"] is False
    assert event["properties"]["$geoip_disable"] is True
