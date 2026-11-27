"""Refresh Google Application Default Credentials outside the event loop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

import httpx
from google.auth import transport

from server.core import shielded

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_lock = asyncio.Lock()
_credentials: Credentials | None = None


class _BoundedRequest(transport.Request):
    def __init__(self, request: Callable[..., transport.Response], deadline: float) -> None:
        self._request = request
        self._deadline = deadline

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> transport.Response:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Vertex AI credential refresh exceeded its request deadline")
        return self._request(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=min(timeout, remaining) if timeout is not None else remaining,
            **kwargs,
        )


async def headers(url: str, timeout_s: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    await _lock.acquire()
    # The refresh owns the lock even when its caller is cancelled.
    return await shielded.shielded(_refresh(url, deadline), name="vertex.credentials")


async def _refresh(url: str, deadline: float) -> dict[str, str]:
    try:
        return await asyncio.to_thread(_authorize, url, deadline)
    finally:
        _lock.release()


def _authorize(url: str, deadline: float) -> dict[str, str]:
    global _credentials
    if time.monotonic() >= deadline:
        raise TimeoutError("Vertex AI credential refresh exceeded its request deadline")
    from google.auth import default
    from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError, TransportError
    from google.auth.transport.requests import Request
    from requests import Session

    with Session() as session:
        request = _BoundedRequest(Request(session), deadline)
        try:
            if _credentials is None:
                _credentials, _ = default(scopes=[_SCOPE], request=request)
            result: dict[str, str] = {}
            before_request: Callable[[transport.Request, str, str, dict[str, str]], None] = (
                _credentials.before_request
            )
            before_request(request, "POST", url, result)
        except DefaultCredentialsError:
            raise RuntimeError(
                "Vertex AI needs Google Application Default Credentials. "
                "Configure Workload Identity."
            ) from None
        except TransportError:
            raise httpx.ConnectError("Could not reach the Google credential service") from None
        except GoogleAuthError:
            raise RuntimeError(
                "Vertex AI could not refresh Google credentials. Check IAM."
            ) from None
    if not result.get("authorization"):
        raise RuntimeError("Google Application Default Credentials returned no access token")
    return result
