"""Bounded Drive change-channel requests using an existing read-only token."""

from __future__ import annotations

import time
from typing import Any

from server.clients._http import get_client
from server.clients._vendor import VendorError, bearer_get, raise_for_status, request_json

_ROOT = "https://www.googleapis.com/drive/v3"


async def member_drives(token: str) -> set[str]:
    """The complete small membership set; a partial listing refuses push setup."""
    result = await bearer_get(
        get_client(),
        f"{_ROOT}/drives",
        token,
        "push corpora",
        params={"pageSize": 20, "fields": "nextPageToken,drives(id)"},
    )
    drives = result.get("drives", [])
    if result.get("nextPageToken") or not isinstance(drives, list) or len(drives) >= 20:
        raise VendorError("push corpus bound exceeded")
    ids = {str(d["id"]) for d in drives if isinstance(d, dict) and d.get("id")}
    if len(ids) != len(drives):
        raise VendorError("invalid push corpora")
    return ids


async def watch(
    token: str, *, channel_id: str, channel_token: str, address: str, drive_id: str
) -> dict[str, Any]:
    params = {"supportsAllDrives": "true", **({"driveId": drive_id} if drive_id else {})}
    start = await bearer_get(
        get_client(), f"{_ROOT}/changes/startPageToken", token, "push cursor", params=params
    )
    cursor = start.get("startPageToken")
    if not isinstance(cursor, str) or not cursor:
        raise VendorError("missing push cursor")
    expires_ms = int((time.time() + 6 * 86400) * 1000)
    answer = await request_json(
        get_client(),
        "POST",
        f"{_ROOT}/changes/watch",
        "push channel",
        headers={"Authorization": f"Bearer {token}"},
        params={**params, "includeItemsFromAllDrives": "true", "pageToken": cursor},
        json={
            "id": channel_id,
            "type": "web_hook",
            "address": address,
            "token": channel_token,
            "expiration": str(expires_ms),
        },
    )
    resource = answer.get("resourceId")
    try:
        expiration = int(answer.get("expiration", 0)) / 1000
    except (TypeError, ValueError):
        raise VendorError("invalid push expiration") from None
    if (
        answer.get("id") != channel_id
        or not isinstance(resource, str)
        or not resource
        or not time.time() < expiration <= expires_ms / 1000 + 1
    ):
        raise VendorError("invalid push channel")
    return {
        "id": channel_id,
        "token": channel_token,
        "resource_id": resource,
        "expiration": expiration,
        "drive_id": drive_id,
    }


async def stop(token: str, channel: dict[str, Any]) -> None:
    response = await get_client().post(
        f"{_ROOT}/channels/stop",
        headers={"Authorization": f"Bearer {token}"},
        json={"id": channel["id"], "resourceId": channel["resource_id"]},
    )
    raise_for_status(response, "stop push channel")
