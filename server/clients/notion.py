"""Minimal client for a page store, read with an integration token.

The credential is the integration's token, sent in the `Authorization` header
only, with the API version the shapes below were written against. The host is a
constant here, and every id that goes into a request path is checked first.

Errors carry the HTTP status, never the store's response body, which can name
pages. A rate limit raises `RateLimited`, a refused token or capability
`CredentialRefused` (401, 403), an id the integration cannot see `NotFound`, a
page past the caller's bounds `TooLarge`, and any other failure `NotionError`.
Nothing here retries: the sync worker's next pass is the retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any

import httpx

from server.clients._http import get_client

_API_URL = "https://api.notion.com/v1"
# The API version the request and response shapes here follow.
API_VERSION = "2026-03-11"
# Seconds between two requests to the store, so a turn stays under its average of
# three requests a second per integration.
PAGE_PACE_S = 0.35  # not a setting: the store's limit, the same on every deployment
# Results per listing page.
_PAGE_SIZE = 100  # not a setting: the most the store hands out
# A timestamp as the store reports it (RFC 3339, UTC). Kept here, not shared: it
# guards this client's own query filter, so nothing else reaches the request body.
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")
# An id in the store's dashed form, the form a source's config stores.
_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Block types whose children are other pages or databases, never read into a page.
_OTHER_PAGES = {"child_page", "child_database"}


class NotionError(RuntimeError):
    """`status` is the HTTP status the store answered, 0 when it gave none (a
    network error, a body that is not JSON, a bound passed here)."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    @property
    def refused(self) -> bool:
        """The store refused this request (a 4xx), as opposed to failing to answer."""
        return 400 <= self.status < 500


class RateLimited(NotionError):
    pass


class CredentialRefused(NotionError):
    pass


class NotFound(NotionError):
    pass


class TooLarge(NotionError):
    pass


def valid_id(value: str) -> bool:
    """Whether `value` is an id in the store's dashed form, safe in a request path."""
    return bool(_ID_RE.fullmatch(value))


def valid_stamp(stamp: str) -> bool:
    """Whether `stamp` is a timestamp as the store reports it."""
    return bool(_STAMP_RE.fullmatch(stamp))


def valid_token(credential: str) -> bool:
    """Whether `credential` can be an integration token: one printable word."""
    return bool(re.fullmatch(r"[\x21-\x7e]{8,256}", credential))


def _raise_for(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 429:
        raise RateLimited(f"{what}: rate_limited", status=429)
    if resp.status_code in (401, 403):
        raise CredentialRefused(f"{what}: http_{resp.status_code}", status=resp.status_code)
    if resp.status_code == 404:
        raise NotFound(f"{what}: http_404", status=404)
    if resp.status_code >= 400:
        raise NotionError(f"{what}: http_{resp.status_code}", status=resp.status_code)


# When the next request of each integration may go out (time.monotonic), by a digest
# of its token: the store limits each integration on its own, so two integrations do
# not wait on each other.
_next_request_at: dict[str, float] = {}


async def _pace(token: str) -> None:
    """Wait until PAGE_PACE_S has passed since this integration's previous request."""
    now = time.monotonic()
    if len(_next_request_at) > 64:
        # Forget the integrations that are no longer waiting, so the map stays small.
        for key, at in list(_next_request_at.items()):
            if at <= now:
                del _next_request_at[key]
    key = hashlib.sha256(token.encode()).hexdigest()[:16]
    start = max(now, _next_request_at.get(key, 0.0))
    _next_request_at[key] = start + PAGE_PACE_S
    if start > now:
        await asyncio.sleep(start - now)


async def _call(
    method: str, path: str, token: str, what: str, *, params: Any = None, body: Any = None
) -> dict[str, Any]:
    await _pace(token)
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": API_VERSION}
    try:
        resp = await get_client().request(
            method, f"{_API_URL}{path}", headers=headers, params=params, json=body
        )
    except httpx.RequestError as e:
        raise NotionError(f"{what}: {type(e).__name__}") from e
    _raise_for(resp, what)
    try:
        data = resp.json()
    except ValueError:
        raise NotionError(f"{what}: body is not JSON") from None
    if not isinstance(data, dict):
        raise NotionError(f"{what}: body is not an object")
    return data


def _checked(object_id: str) -> str:
    if not valid_id(object_id):
        raise ValueError("not an id")
    return object_id


async def page(token: str, page_id: str) -> dict[str, Any]:
    """A page's properties, address, last edit and trash flag."""
    return await _call("GET", f"/pages/{_checked(page_id)}", token, f"page {page_id}")


async def database(token: str, database_id: str) -> dict[str, Any]:
    """A database's title, trash flag and data sources."""
    return await _call(
        "GET", f"/databases/{_checked(database_id)}", token, f"database {database_id}"
    )


def data_source_ids(db: dict[str, Any]) -> list[str]:
    """The ids of a database's data sources, the tables its pages live in."""
    ids = [str(d.get("id") or "") for d in db.get("data_sources") or [] if isinstance(d, dict)]
    return [i for i in ids if valid_id(i)]


def _query_body(edited_after: str) -> dict[str, Any]:
    if edited_after and not valid_stamp(edited_after):
        raise ValueError("edited_after is not a timestamp")
    body: dict[str, Any] = {
        "page_size": _PAGE_SIZE,
        "result_type": "page",
        "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}],
    }
    if edited_after:
        body["filter"] = {
            "timestamp": "last_edited_time",
            "last_edited_time": {"after": edited_after},
        }
    return body


async def _query(token: str, data_source_id: str, body: dict[str, Any]) -> dict[str, Any]:
    path = f"/data_sources/{_checked(data_source_id)}/query"
    return await _call("POST", path, token, f"data source {data_source_id}", body=body)


def _pages_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        r for r in data.get("results") or [] if isinstance(r, dict) and r.get("object") == "page"
    ]


async def first_pages(token: str, data_source_id: str) -> tuple[list[dict[str, Any]], bool]:
    """The first listing page (100 at most) of a data source's pages, newest edit
    first, and whether more follow."""
    body = _query_body("")
    body["sorts"] = [{"timestamp": "last_edited_time", "direction": "descending"}]
    data = await _query(token, data_source_id, body)
    return _pages_of(data), bool(data.get("has_more"))


async def query_pages(
    token: str, data_source_id: str, *, edited_after: str = ""
) -> tuple[list[dict[str, Any]], bool]:
    """Every page of a database's data source, oldest edit first, or only those edited
    after `edited_after` (a `valid_stamp`), and whether the store returned them all:
    False when it says its result is incomplete. A failure on any listing page
    raises, so every page was read."""
    body = _query_body(edited_after)
    pages: list[dict[str, Any]] = []
    whole = True
    while True:
        data = await _query(token, data_source_id, body)
        status = data.get("request_status")
        whole = whole and not (isinstance(status, dict) and status.get("type") == "incomplete")
        pages.extend(_pages_of(data))
        cursor = data.get("next_cursor")
        if not data.get("has_more") or not isinstance(cursor, str) or not cursor:
            return pages, whole
        body = {**body, "start_cursor": cursor}


async def page_blocks(token: str, page_id: str, *, max_requests: int) -> list[dict[str, Any]]:
    """A page's blocks in order, each block with children carrying them under
    `children`; child pages and databases are not read into it. A synced copy whose
    original the integration cannot see (404, 403) is marked `unreadable` and read
    no further. TooLarge once the walk would take more than `max_requests`
    requests."""
    requests = 0

    async def children(block_id: str) -> list[dict[str, Any]]:
        nonlocal requests
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"page_size": _PAGE_SIZE}
        while True:
            if requests >= max_requests:
                raise TooLarge(f"page {page_id}: over {max_requests} requests")
            requests += 1
            data = await _call(
                "GET",
                f"/blocks/{_checked(block_id)}/children",
                token,
                f"page {page_id}",
                params=params,
            )
            out.extend(b for b in data.get("results") or [] if isinstance(b, dict))
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not isinstance(cursor, str) or not cursor:
                break
            params = {**params, "start_cursor": cursor}
        for block in out:
            # A synced copy's content lives under the block it was synced from.
            synced = (block.get("synced_block") or {}).get("synced_from") or {}
            if block.get("type") in _OTHER_PAGES or not (block.get("has_children") or synced):
                continue
            nested = str(synced.get("block_id") or block.get("id") or "")
            if not valid_id(nested):
                continue
            try:
                block["children"] = await children(nested)
            except (NotFound, CredentialRefused) as e:
                # Only a copy's original lives elsewhere; any other block's is the page's.
                if not synced or e.status == 401:
                    raise
                block["unreadable"] = True
        return out

    return await children(page_id)


def title_of(obj: dict[str, Any]) -> str:
    """A page's title (its `title` property, whatever it is named) or a database's."""
    rich = obj.get("title")
    if not isinstance(rich, list):
        rich = next(
            (
                p.get("title")
                for p in (obj.get("properties") or {}).values()
                if isinstance(p, dict) and p.get("type") == "title"
            ),
            [],
        )
    return "".join(str(t.get("plain_text") or "") for t in rich or [] if isinstance(t, dict))


__all__ = [
    "API_VERSION",
    "PAGE_PACE_S",
    "CredentialRefused",
    "NotFound",
    "NotionError",
    "RateLimited",
    "TooLarge",
    "data_source_ids",
    "database",
    "first_pages",
    "page",
    "page_blocks",
    "query_pages",
    "title_of",
    "valid_id",
    "valid_stamp",
    "valid_token",
]
