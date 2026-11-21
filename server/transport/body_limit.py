"""Request-body cap — one bounded input at the front door.

Loom's most untrusted input is the request body, and nothing else bounds
it: uvicorn has no default body limit, the JSON parser reads whatever
arrives, and a Pydantic `max_length` only runs after the whole body is in
memory.

This is a pure ASGI middleware, so it costs nothing per request beyond one
header read:

* a declared `Content-Length` above the cap is refused at once with
  `413 PAYLOAD_TOO_LARGE` in the same `{"detail": {code, message}}` shape
  every other loom error uses — the body is never read;
* a chunked / undeclared body is counted as it streams and cut off at the
  cap with the same 413; an overflow aborts the downstream body read
  before a complete JSON prefix can reach a handler.

The cap is `LOOM_MAX_REQUEST_BYTES` (32 MiB by default — a 400-message
`memory.set_from_messages` is a few hundred KB, the largest `memory.import`
about 10 MB of printable ASCII text, more when its text is non-ASCII or control
characters). Websocket and lifespan scopes pass through untouched, and so do
the paths in `own_limit`, whose route streams the body against a cap of its
own (`POST /v1/knowledge.upload`, `LOOM_KNOWLEDGE_UPLOAD_MAX_BYTES`). Fixed cell
administration caps still apply when the global cap is disabled; a positive
global cap can only make those limits stricter.
"""

from __future__ import annotations

import json
from typing import Any

from server.core.errors import classified

CODE = "PAYLOAD_TOO_LARGE"

_CELL_BODY_LIMITS = {
    "/v1/admin/cell.capabilities": 1024,
    "/v1/admin/cell.settings_list": 65536,
    "/v1/admin/cell.settings_set": 65536,
    "/v1/admin/cell.settings_history": 65536,
    "/v1/admin/cell.signing_keys_list": 65536,
    "/v1/admin/cell.signing_keys_rotate": 65536,
    "/v1/admin/cell.signing_keys_retire": 65536,
    "/v1/admin/cell.subject_revoke": 65536,
    "/v1/admin/cell.ratelimit_namespace_set": 65536,
    "/v1/admin/cell.budget_set": 65536,
    "/v1/admin/cell.blacklist_global_add": 65536,
    "/v1/admin/cell.blacklist_global_list": 65536,
    "/v1/admin/cell.blacklist_global_remove": 65536,
}


def _too_large_response(limit: int, declared: int | None) -> tuple[dict[str, Any], bytes]:
    msg = f"request body exceeds limit ({limit} bytes)" + (
        f"; declared {declared}" if declared is not None else ""
    )
    detail = classified({"code": CODE, "message": msg, "limit_bytes": limit}, 413)
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    return {"type": "http.response.start", "status": 413, "headers": headers}, body


class _BodyTooLarge(Exception):
    """Stop body parsing without exposing a valid truncated prefix."""


class BodyLimitMiddleware:
    """Refuse bodies above the global and fixed administration caps."""

    def __init__(self, app: Any, max_bytes: int, own_limit: tuple[str, ...] = ()) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)
        self.own_limit = frozenset(own_limit)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        limit = self.max_bytes
        fixed_limit = _CELL_BODY_LIMITS.get(scope.get("path"))
        if fixed_limit is not None:
            limit = min(limit, fixed_limit) if limit > 0 else fixed_limit
        if (
            scope.get("type") != "http"
            or limit <= 0
            or (fixed_limit is None and scope.get("path") in self.own_limit)
        ):
            await self.app(scope, receive, send)
            return

        declared: int | None = None
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            start, body = _too_large_response(limit, declared)
            await send(start)
            await send({"type": "http.response.body", "body": body})
            return

        seen = 0
        tripped = False

        async def counting_receive() -> Any:
            nonlocal seen, tripped
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    tripped = True
                    raise _BodyTooLarge
            return message

        sent_start = False

        async def guarded_send(message: Any) -> None:
            nonlocal sent_start
            if tripped and not sent_start and message.get("type") == "http.response.start":
                start, body = _too_large_response(limit, None)
                sent_start = True
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return
            if tripped and sent_start:
                return  # the 413 already went out; drop the app's response
            if message.get("type") == "http.response.start":
                sent_start = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            if not sent_start:
                start, body = _too_large_response(limit, None)
                await send(start)
                await send({"type": "http.response.body", "body": body})


__all__ = ["CODE", "BodyLimitMiddleware"]
