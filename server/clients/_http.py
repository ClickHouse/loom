"""Process-wide shared httpx.AsyncClients for provider APIs.

Two pools, each built lazily on first use: `get_model_client()` for model
calls (the LLM and embedding providers, the inference gateway's token
endpoint), and `get_client()` for everything else (GitHub, an issuer's
discovery document). A pool keeps the socket count flat and skips a handshake
per call; the lifespan in server/main.py calls `aclose()` at shutdown.
Callers pass their own per-request timeout when the default is too short.

Both follow redirects, so neither is for caller-supplied URLs: a client that
must not be redirected to another host (webhook delivery) builds its own, and a
model call to one (a namespace's classifier) passes `follow_redirects=False`.
The model client runs `_on_request` before every request it sends, each
redirect hop included: while the gateway is required no model call leaves for
a host LOOM_INFERENCE_GATEWAY_HOSTS does not list, the identity headers go only
to a host it lists, and a job's call for an identity the gateway holds is
refused unsent (`server.llm.gateway.sending`). `_on_response` runs after every
answer, which tells the gateway's bearer and its identity holds what it said
(`server.llm.gateway.answered`).
"""

from __future__ import annotations

import threading
from typing import Any

import httpx

from server.core.errors import GatewayRequiredError
from server.settings import settings
from server.settings.catalog import url_host

_lock = threading.Lock()
_client: httpx.AsyncClient | None = None
_model_client: httpx.AsyncClient | None = None

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


def refuse_off_gateway(url: str) -> None:
    """Refuses a send to `url` while the gateway is required and does not list
    its host, whatever named it (a model named per request, the dashboard's
    check, a redirect, a namespace's endpoint): neither a key nor content goes there."""
    host = settings.off_gateway(url)
    if host:
        raise GatewayRequiredError(
            f"{host} is not in LOOM_INFERENCE_GATEWAY_HOSTS: model calls go only to the gateway"
        )


async def _on_request(request: httpx.Request) -> None:
    refuse_off_gateway(str(request.url))
    identity = settings.identity_headers()
    if identity and url_host(str(request.url)) not in settings.gateway_hosts():
        # Whom a call is for is the gateway's to know: a reranker, a classifier or a
        # provider elsewhere never gets the identity headers, a redirect hop included.
        for name, _ in identity:
            request.headers.pop(name, None)
    from server.llm import gateway

    await gateway.sending(request)


async def _on_response(response: httpx.Response) -> None:
    from server.llm import gateway

    await gateway.answered(response)


def _build(**extra: Any) -> httpx.AsyncClient:
    return _ProviderClient(
        timeout=httpx.Timeout(
            settings.http_client_timeout_s, connect=settings.http_client_connect_timeout_s
        ),
        limits=httpx.Limits(
            max_connections=settings.http_client_max_connections,
            max_keepalive_connections=settings.http_client_max_keepalive_connections,
        ),
        # Follow a provider's trailing-slash or moved-endpoint redirect.
        follow_redirects=True,
        **extra,
    )


def get_client() -> httpx.AsyncClient:
    """Return the shared client for calls that are not model calls."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = _build()
    return _client


def get_model_client() -> httpx.AsyncClient:
    """Return the shared client for model calls, its gateway hook in place."""
    global _model_client
    if _model_client is not None:
        return _model_client
    with _lock:
        if _model_client is None:
            _model_client = _build(
                event_hooks={"request": [_on_request], "response": [_on_response]}
            )
    return _model_client


async def aclose() -> None:
    """Drain both connection pools. Idempotent; safe at shutdown."""
    global _client, _model_client
    clients = [c for c in (_client, _model_client) if c is not None]
    _client = _model_client = None
    for c in clients:
        await c.aclose()
