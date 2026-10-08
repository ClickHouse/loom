"""The JSON POST every bench harness sends to a Loom server, sync and async.

One request policy:

* `Authorization: Bearer <token>` when a token is given; otherwise the
  client's own headers (a client built with auth headers passes no token).
* A 429 waits out `Retry-After` (seconds; 1 s when absent or unreadable,
  clamped to [0.2, 60]) and retries, up to `waits_429` times. A rate-limited
  call is not an errored one, and waits do not use up the 5xx budget.
* A 5xx is retried until `attempts` requests have been sent, with a
  0.5 * 2^n s backoff between them. `attempts=1` sends one request.
* Any other status >= 400, or the last 429 / 5xx, raises
  `httpx.HTTPStatusError` whose message carries the start of the response
  body, so a refusal says why in the bench's error column.
* Transport errors (refused, reset, timed out) propagate unchanged: a harness
  that rides out a server restart keeps its own loop.

`post` / `apost` return the `httpx.Response` (for a caller that reads headers
or times the call); `post_json` / `apost_json` return the decoded body.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

RETRY_AFTER_DEFAULT_S = 1.0
RETRY_AFTER_MIN_S = 0.2
RETRY_AFTER_MAX_S = 60.0
BODY_EXCERPT_CHARS = 400


def retry_after_s(r: httpx.Response) -> float:
    """The wait a 429 asks for, clamped to [RETRY_AFTER_MIN_S, RETRY_AFTER_MAX_S]."""
    try:
        wait: float = float(r.headers.get("retry-after") or RETRY_AFTER_DEFAULT_S)
    except ValueError:
        wait = RETRY_AFTER_DEFAULT_S
    return float(min(max(wait, RETRY_AFTER_MIN_S), RETRY_AFTER_MAX_S))


def backoff_5xx_s(attempt: int) -> float:
    """The pause after the `attempt`-th (0-based) 5xx."""
    return 0.5 * 2.0**attempt


def raise_for_status(r: httpx.Response) -> None:
    """`Response.raise_for_status` with the start of the body in the message."""
    if r.status_code < 400:
        return
    try:
        excerpt = r.text[:BODY_EXCERPT_CHARS]
    except Exception as e:  # noqa: BLE001 — an undecodable body must not hide the status
        excerpt = f"<body unreadable: {type(e).__name__}>"
    raise httpx.HTTPStatusError(
        f"{r.status_code} {r.reason_phrase} from {r.request.url}: {excerpt}",
        request=r.request,
        response=r,
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _next_wait(r: httpx.Response, sent: int, waited: int, attempts: int, waits_429: int) -> float:
    """Seconds to sleep before resending, or -1 when `r` is the answer."""
    if r.status_code == 429 and waited < waits_429:
        return retry_after_s(r)
    if r.status_code >= 500 and sent < attempts:
        return backoff_5xx_s(sent - 1)
    return -1.0


def post(
    client: httpx.Client,
    url: str,
    body: Any,
    *,
    token: str = "",
    timeout_s: float = 60.0,
    attempts: int = 1,
    waits_429: int = 8,
) -> httpx.Response:
    """POST `body` as JSON under the module's policy; the successful response."""
    sent = waited = 0
    while True:
        r = client.post(url, json=body, headers=_headers(token), timeout=timeout_s)
        if r.status_code != 429:
            sent += 1
        wait = _next_wait(r, sent, waited, max(1, attempts), waits_429)
        if wait < 0:
            raise_for_status(r)
            return r
        waited += r.status_code == 429
        time.sleep(wait)


async def apost(
    client: httpx.AsyncClient,
    url: str,
    body: Any,
    *,
    token: str = "",
    timeout_s: float = 60.0,
    attempts: int = 1,
    waits_429: int = 8,
) -> httpx.Response:
    """`post` for an `httpx.AsyncClient`."""
    sent = waited = 0
    while True:
        r = await client.post(url, json=body, headers=_headers(token), timeout=timeout_s)
        if r.status_code != 429:
            sent += 1
        wait = _next_wait(r, sent, waited, max(1, attempts), waits_429)
        if wait < 0:
            raise_for_status(r)
            return r
        waited += r.status_code == 429
        await asyncio.sleep(wait)


def post_json(client: httpx.Client, url: str, body: Any, **kw: Any) -> Any:
    """`post`, returning the decoded JSON body."""
    return post(client, url, body, **kw).json()


async def apost_json(client: httpx.AsyncClient, url: str, body: Any, **kw: Any) -> Any:
    """`apost`, returning the decoded JSON body."""
    return (await apost(client, url, body, **kw)).json()
