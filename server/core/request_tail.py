"""Work a request must finish before its answer leaves the server.

Without sticky sessions the caller's next request may land on another replica,
which sees only what is committed in ClickHouse. A step whose effect that request
must see (a ledger row a history-policy rule counts, the release of a claim taken
for the request) is handed to `defer`; the ASGI middleware runs every deferred
step when the response starts, so the caller cannot read the answer before they
are done. Outside a request (`defer` returns False) the caller runs it itself.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from server.core.ch import current_query_tag, query_tag

log = logging.getLogger("loom.request_tail")


class _Tail:
    __slots__ = ("closed", "keys", "steps")

    def __init__(self) -> None:
        # Each step with the query tag of the request that deferred it: the
        # middleware runs it outside the request's own context.
        self.steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        self.keys: set[str] = set()
        self.closed = False


_tail: ContextVar[_Tail | None] = ContextVar("loom_request_tail", default=None)


def active() -> bool:
    tail = _tail.get()
    return tail is not None and not tail.closed


def defer(step: Callable[[], Awaitable[Any]]) -> bool:
    """Run `step` before this request's answer starts. False outside a request,
    or once the answer has started: the caller runs it then."""
    tail = _tail.get()
    if tail is None or tail.closed:
        return False
    tail.steps.append((current_query_tag(), step))
    return True


def defer_once(key: str, step: Callable[[], Awaitable[Any]]) -> bool:
    """`defer`, once per `key` per request (True when it is, or already was, deferred)."""
    tail = _tail.get()
    if tail is None or tail.closed:
        return False
    if key not in tail.keys:
        tail.keys.add(key)
        tail.steps.append((current_query_tag(), step))
    return True


def deferred(key: str) -> bool:
    """Whether a step was deferred under `key` in this request."""
    tail = _tail.get()
    return tail is not None and key in tail.keys


async def settle(tail: _Tail) -> None:
    """Run the deferred steps once, in order; a failing step is logged, never raised."""
    if tail.closed:
        return
    tail.closed = True
    for tag, step in tail.steps:
        try:
            with query_tag(tag):
                await step()
        except Exception as e:  # noqa: BLE001 — the answer still goes out
            log.warning("request tail step failed: %s", e)
    tail.steps.clear()


class RequestTailMiddleware:
    """ASGI: opens a tail per HTTP request and settles it before the response starts
    (or when the app ends without one)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        tail = _Tail()
        token = _tail.set(tail)

        async def _send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                await settle(tail)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            await settle(tail)
            _tail.reset(token)


__all__ = ["RequestTailMiddleware", "active", "defer", "defer_once", "deferred", "settle"]
