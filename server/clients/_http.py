"""Process-wide shared httpx.AsyncClient for provider APIs.

One pool for the LLM and embedding providers and GitHub keeps the socket
count flat and skips a handshake per call. Built lazily on first
`get_client()`; the lifespan in server/main.py calls `aclose()` at shutdown.
Callers pass their own per-request timeout when the default is too short.

It follows redirects, so it is not for caller-supplied URLs: a client that
must not be redirected to another host (webhook delivery) builds its own.
"""

from __future__ import annotations

import threading

import httpx

_lock = threading.Lock()
_client: httpx.AsyncClient | None = None

# Provider credentials that ride a header other than Authorization.
_KEY_HEADERS = ("x-api-key", "x-goog-api-key", "api-key")


class _ProviderClient(httpx.AsyncClient):
    """httpx drops `Authorization` when a redirect leaves the origin; the
    Anthropic and Gemini keys ride `x-api-key` / `x-goog-api-key`, which it
    would forward to whatever host the redirect names. This client drops
    those too, on any redirect that changes scheme, host or port."""

    def _redirect_headers(
        self, request: httpx.Request, url: httpx.URL, method: str
    ) -> httpx.Headers:
        headers = super()._redirect_headers(request, url, method)
        src = request.url
        if (url.scheme, url.host, url.port) != (src.scheme, src.host, src.port):
            for name in _KEY_HEADERS:
                headers.pop(name, None)
        return headers


def get_client() -> httpx.AsyncClient:
    """Return the shared client, constructing it on first use."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = _ProviderClient(
                # 20s default — long enough for embedding APIs which can
                # be slow on cold paths, short enough that a stalled call
                # doesn't pin the request thread indefinitely.
                timeout=httpx.Timeout(20.0, connect=5.0),
                # Bound the pool so a single misbehaving downstream can't
                # exhaust sockets; 100 covers batched embedding traffic at peak.
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20,
                ),
                # Follow a provider's trailing-slash or moved-endpoint redirect.
                follow_redirects=True,
            )
    return _client


async def aclose() -> None:
    """Drain the connection pool. Idempotent; safe at shutdown."""
    global _client
    if _client is None:
        return
    c = _client
    _client = None
    await c.aclose()
