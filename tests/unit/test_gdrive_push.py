"""Drive channel requests use the provider API and bounded complete corpora."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.clients import gdrive_push as client
from server.clients._vendor import VendorError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result,expected", [({}, set()), ({"drives": [{"id": "drive"}]}, {"drive"})]
)
async def test_complete_memberships_include_empty_account(
    monkeypatch: pytest.MonkeyPatch, result: dict, expected: set
) -> None:
    monkeypatch.setattr(client, "bearer_get", AsyncMock(return_value=result))
    assert await client.member_drives("token") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"nextPageToken": "more"},
        {"drives": [{"id": str(i)} for i in range(20)]},
        {"drives": [{}]},
        {"drives": "wrong"},
    ],
)
async def test_incomplete_or_over_limit_memberships_refuse(
    monkeypatch: pytest.MonkeyPatch, result: dict
) -> None:
    monkeypatch.setattr(client, "bearer_get", AsyncMock(return_value=result))
    with pytest.raises(VendorError):
        await client.member_drives("token")


@pytest.mark.asyncio
async def test_shared_drive_cursor_and_watch_name_same_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client.time, "time", lambda: 1000)
    cursor = AsyncMock(return_value={"startPageToken": "cursor"})
    watch = AsyncMock(
        return_value={"id": "channel", "resourceId": "resource", "expiration": "2000000"}
    )
    monkeypatch.setattr(client, "bearer_get", cursor)
    monkeypatch.setattr(client, "request_json", watch)
    result = await client.watch(
        "token",
        channel_id="channel",
        channel_token="proof",
        address="https://loom.example/hook",
        drive_id="drive",
    )
    assert cursor.call_args.kwargs["params"]["driveId"] == "drive"
    assert watch.call_args.kwargs["params"]["driveId"] == "drive"
    assert watch.call_args.kwargs["params"]["pageToken"] == "cursor"
    assert watch.call_args.kwargs["json"]["token"] == "proof"
    assert result["resource_id"] == "resource" and result["expiration"] == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"id": "other", "resourceId": "resource", "expiration": "2000000"},
        {"id": "channel", "expiration": "2000000"},
        {"id": "channel", "resourceId": "resource", "expiration": "bad"},
    ],
)
async def test_watch_response_must_bind_requested_channel(
    monkeypatch: pytest.MonkeyPatch, response: dict
) -> None:
    monkeypatch.setattr(client.time, "time", lambda: 1000)
    monkeypatch.setattr(client, "bearer_get", AsyncMock(return_value={"startPageToken": "cursor"}))
    monkeypatch.setattr(client, "request_json", AsyncMock(return_value=response))
    with pytest.raises(VendorError):
        await client.watch(
            "token",
            channel_id="channel",
            channel_token="proof",
            address="https://loom.example/hook",
            drive_id="",
        )
