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


def test_domain_sharing_is_enabled_by_default() -> None:
    assert Settings.model_fields["adoption_telemetry_share_domains"].default is True


async def test_explicit_opt_out_skips_adoption_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings.model_fields["adoption_telemetry_enabled"].default is True
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", False)
    ch = AsyncMock()
    http = AsyncMock()
    monkeypatch.setattr(adoption_telemetry, "get_client", ch)
    monkeypatch.setattr(adoption_telemetry, "get_http_client", http)

    assert "adoption_telemetry" not in {worker.name for worker in enabled_workers()}
    await adoption_telemetry.run_pass()
    ch.assert_not_called()
    http.assert_not_called()


def test_adoption_worker_uses_the_configured_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(settings, "adoption_telemetry_interval_seconds", 120)
    worker = next(worker for worker in enabled_workers() if worker.name == "adoption_telemetry")
    assert worker.interval_seconds == 120


async def test_missing_deployment_id_surfaces_status_without_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
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
        httpx.Response(200, json={"stored": True, "window_start": "not-this-window"}),
        None,
    ],
    ids=["redirect", "wrong-acknowledgement", "oversized-acknowledgement"],
)
@pytest.mark.parametrize("share_domains", [False, True])
async def test_delivery_retries_without_recounting_after_ack(
    monkeypatch: pytest.MonkeyPatch, rejection: httpx.Response | None, share_domains: bool
) -> None:
    monkeypatch.setattr(settings, "adoption_telemetry_enabled", True)
    monkeypatch.setattr(settings, "adoption_telemetry_share_domains", share_domains)
    monkeypatch.setattr(
        adoption_telemetry, "utc_now", lambda: datetime(2026, 11, 24, 3, tzinfo=UTC)
    )
    deployment_id = uuid4()
    authority = AsyncMock(return_value={"deployment_id": str(deployment_id)})
    monkeypatch.setattr(adoption_telemetry, "recorded_authority", authority)
    monkeypatch.setattr(adoption_telemetry, "get_client", object)
    restricted_pairs = {("test", "restricted")}

    def configured_pairs() -> set[tuple[str, str]]:
        assert share_domains
        return restricted_pairs

    monkeypatch.setattr(adoption_telemetry, "configured_restricted_pairs", configured_pairs)

    async def report(
        start: datetime,
        end: datetime,
        installation_id: UUID,
        *,
        restricted: set[tuple[str, str]] | None,
        ch: object,
    ) -> dict[str, str]:
        assert installation_id != deployment_id
        assert start == datetime(2026, 11, 23, 12, tzinfo=UTC)
        assert end == datetime(2026, 11, 24, tzinfo=UTC)
        assert restricted == (restricted_pairs if share_domains else None)
        return {"installation_id": str(installation_id), "window_start": start.isoformat()}

    aggregate = AsyncMock(side_effect=report)
    monkeypatch.setattr(adoption_telemetry, "window_report", aggregate)
    requests: list[httpx.Request] = []

    def collect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        acknowledgement = {"stored": True, **json.loads(request.content)}
        if len(requests) > 1:
            return httpx.Response(200, json=acknowledgement)
        if rejection is not None:
            return rejection
        padded = json.dumps(acknowledgement).encode().ljust(4097, b" ")
        return httpx.Response(200, content=padded)

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
    assert all(request.url.host == "loom.clickhouse.com" for request in requests)
    assert all(request.url.path == "/api/v1/adoption" for request in requests)
    assert requests[0].content == requests[1].content
    payload = json.loads(requests[0].content)
    assert UUID(payload["installation_id"]).version == 5
    assert payload["window_start"] == "2026-11-23T12:00:00+00:00"
