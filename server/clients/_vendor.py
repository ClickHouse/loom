"""What the knowledge-source clients share: their errors, the mapping from an HTTP
answer to one, a JSON request, a token exchange and a size-capped body read.

Errors carry the HTTP status, never the vendor's response body, which can name
documents, people or the vendor's own hosts. A rate limit raises `RateLimited`, a
refused credential `CredentialRefused`, an id the credential cannot see `NotFound`,
a body over the caller's size bound `TooLarge`, and any other failure
`VendorError`. Nothing here retries: the sync worker's next pass is the retry.

Each helper takes the `httpx.AsyncClient` to send with, so a client module keeps
its own `get_client` name and a test replaces it there.
"""

from __future__ import annotations

from typing import Any

import httpx


class VendorError(RuntimeError):
    """`status` is the HTTP status the vendor answered, 0 when it gave none (a
    network error, a body that is not JSON, a bound passed here)."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    @property
    def refused(self) -> bool:
        """The vendor refused this request (a 4xx), as opposed to failing to answer."""
        return 400 <= self.status < 500


class RateLimited(VendorError):
    pass


class CredentialRefused(VendorError):
    pass


class NotFound(VendorError):
    pass


class TooLarge(VendorError):
    pass


# The `reason` (or `status`) an error body gives a 403 that is a rate limit, not a
# refusal; compared lower-cased.
_RATE_LIMIT_REASONS = {
    "ratelimitexceeded",
    "userratelimitexceeded",
    "dailylimitexceeded",
    "quotaexceeded",
    "rate_limit_exceeded",
    "resource_exhausted",
}


def _error_reasons(resp: httpx.Response) -> set[str]:
    """The lower-cased `error.errors[].reason` and `error.status` of an error body, or
    an OAuth token endpoint's `error` string."""
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return set()
    if isinstance(error, str):
        return {error.lower()} if error else set()
    if not isinstance(error, dict):
        return set()
    reasons = {str(e.get("reason") or "") for e in error.get("errors") or [] if isinstance(e, dict)}
    reasons.add(str(error.get("status") or ""))
    return {r.lower() for r in reasons if r}


def rate_limited(resp: httpx.Response) -> bool:
    """A 429, or a 403 whose error reason is a rate limit rather than a refusal."""
    return resp.status_code == 429 or (
        resp.status_code == 403 and bool(_error_reasons(resp) & _RATE_LIMIT_REASONS)
    )


def raise_for_status(resp: httpx.Response, what: str) -> None:
    """The error an answer of 400 or more stands for; nothing for a success."""
    status = resp.status_code
    if rate_limited(resp):
        raise RateLimited(f"{what}: rate_limited", status=status)
    if status in (401, 403):
        raise CredentialRefused(f"{what}: http_{status}", status=status)
    if status == 404:
        raise NotFound(f"{what}: http_404", status=404)
    if status >= 400:
        raise VendorError(f"{what}: http_{status}", status=status)


def json_object(resp: httpx.Response, what: str) -> dict[str, Any]:
    """The answer's body, which must be a JSON object."""
    try:
        data = resp.json()
    except ValueError:
        raise VendorError(f"{what}: body is not JSON") from None
    if not isinstance(data, dict):
        raise VendorError(f"{what}: body is not an object")
    return data


async def request_json(
    client: httpx.AsyncClient, method: str, url: str, what: str, **kw: Any
) -> dict[str, Any]:
    """One request (`kw` goes to `client.request`) and its JSON object answer."""
    try:
        resp = await client.request(method, url, **kw)
    except httpx.RequestError as e:
        raise VendorError(f"{what}: {type(e).__name__}") from e
    raise_for_status(resp, what)
    return json_object(resp, what)


async def exchange_token(
    client: httpx.AsyncClient, url: str, what: str, **kw: Any
) -> dict[str, Any]:
    """A token endpoint's answer to a POST (`kw` goes to `client.post`), which holds
    an `access_token`."""
    try:
        resp = await client.post(url, **kw)
    except httpx.RequestError as e:
        raise VendorError(f"{what}: {type(e).__name__}") from e
    # A rate limit first; otherwise a token endpoint answers an unknown or revoked
    # credential with 400, 401 or 403.
    if rate_limited(resp):
        raise RateLimited(f"{what}: rate_limited", status=resp.status_code)
    if resp.status_code in (400, 401, 403):
        raise CredentialRefused(f"{what}: http_{resp.status_code}", status=resp.status_code)
    raise_for_status(resp, what)
    answer = json_object(resp, what)
    token = answer.get("access_token")
    if not isinstance(token, str) or not token:
        raise VendorError(f"{what}: no access_token in the answer")
    return answer


async def read_capped(resp: httpx.Response, max_bytes: int) -> bytes | None:
    """A streamed answer's body, or None once it passes `max_bytes`: the rest is
    never read."""
    body = bytearray()
    async for piece in resp.aiter_bytes():
        body.extend(piece)
        if len(body) > max_bytes:
            return None
    return bytes(body)


async def get_capped(
    client: httpx.AsyncClient, url: str, what: str, *, max_bytes: int, **kw: Any
) -> bytes:
    """The body of a GET (`kw` goes to `client.stream`), read up to `max_bytes`;
    TooLarge past it."""
    try:
        async with client.stream("GET", url, **kw) as resp:
            if resp.status_code >= 400:
                await resp.aread()  # the error body tells a rate limit from a refusal
                raise_for_status(resp, what)
            body = await read_capped(resp, max_bytes)
    except httpx.RequestError as e:
        raise VendorError(f"{what}: {type(e).__name__}") from e
    if body is None:
        raise TooLarge(f"{what}: over {max_bytes} bytes")
    return body
