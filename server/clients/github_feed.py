"""Bounded read-only API calls for a team's tracker feed."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from server.clients._http import get_client
from server.clients._vendor import read_capped
from server.clients.github import _headers
from server.core.github_values import PAGE_SIZE, positive_id, repository, since_parameter

API = "https://api.github.com"
PAGE_MAX_BYTES = 4 * 1024 * 1024  # not a setting: fixed memory ceiling for one CLI response
SCAN_MAX_BYTES = 24 * 1024 * 1024  # not a setting: fixed memory ceiling for one CLI scan
STREAMS = {"items": "issues", "comments": "issues/comments", "review_comments": "pulls/comments"}


class FeedProviderError(RuntimeError):
    """A provider response failed without exposing its content."""


@dataclass(frozen=True)
class Batch:
    items: list[dict[str, Any]]
    complete: bool


async def _get(path: str, token: str, params: dict[str, Any] | None = None) -> tuple[Any, int]:
    try:
        # not a setting: fixed per-request ceiling inside the operator CLI turn
        async with asyncio.timeout(15):
            async with get_client().stream(
                "GET", API + path, headers=_headers(token), params=params, follow_redirects=False
            ) as response:
                if response.status_code != 200:
                    raise FeedProviderError(f"GitHub feed http_{response.status_code}")
                body = await read_capped(response, PAGE_MAX_BYTES)
        if body is None:
            raise FeedProviderError("GitHub feed response exceeds the size bound")
        return json.loads(body), len(body)
    except (httpx.RequestError, TimeoutError, ValueError) as exc:
        raise FeedProviderError(f"GitHub feed {type(exc).__name__}") from None


async def metadata(repo: str, token: str) -> dict[str, Any]:
    data, _ = await _get(f"/repos/{repository(repo)}", token)
    if not isinstance(data, dict):
        raise FeedProviderError("GitHub feed expected repository metadata")
    return data


async def pull_request(repo: str, token: str, number: int) -> dict[str, Any]:
    number = positive_id(number)
    data, _ = await _get(f"/repos/{repository(repo)}/pulls/{number}", token)
    if not isinstance(data, dict) or data.get("number") != number:
        raise FeedProviderError("GitHub feed mismatched pull request")
    return data


async def pages(
    repo: str, token: str, stream: str, since: datetime | None, *, max_pages: int
) -> Batch:
    if stream not in STREAMS or not 2 <= max_pages <= 100:
        raise FeedProviderError("GitHub feed requires a known stream and 2..100 max-pages")
    path = f"/repos/{repository(repo)}/{STREAMS[stream]}"
    params: dict[str, Any] = dict(sort="updated", direction="asc", per_page=PAGE_SIZE)
    if stream == "items":
        params["state"] = "all"
    if since is not None:
        params["since"] = since_parameter(since)
    out: list[dict[str, Any]] = []
    size = 0
    for page in range(1, max_pages + 1):
        data, count = await _get(path, token, {**params, "page": page})
        size += count
        if size > SCAN_MAX_BYTES:
            raise FeedProviderError("GitHub feed scan exceeds the size bound")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise FeedProviderError("GitHub feed expected an object list")
        if len(data) > PAGE_SIZE:
            raise FeedProviderError("GitHub feed page exceeds its item bound")
        out.extend(data)
        if len(data) < PAGE_SIZE:
            return Batch(out, complete=True)
        if page < max_pages:
            await asyncio.sleep(0.7)
    return Batch(out, complete=False)
