"""Private HTTP callback for Loom's transient reads."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from .read_service import ReadFailure, ReadService

_CACHE_HEADERS = {"Cache-Control": "no-store"}
_STATUSES = {
    "denied": 403,
    "reconnect": 409,
    "overloaded": 429,
    "timeout": 504,
    "unavailable": 503,
    "unsupported": 422,
}


async def _disconnect(request: Request) -> None:
    while (await request.receive())["type"] != "http.disconnect":
        pass


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"status": code}, status, headers=_CACHE_HEADERS)


class _ReadRoute:
    def __init__(self, service: ReadService, operation: Literal["live", "check_access"]) -> None:
        self.service = service
        self.operation = operation

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        if (
            request.url.query
            or request.headers.get("content-length", "0") != "0"
            or "transfer-encoding" in request.headers
        ):
            await _error("denied", 400)(scope, receive, send)
            return
        auth = request.headers.getlist("authorization")
        if len(auth) != 1 or not auth[0].startswith("Bearer "):
            await _error("denied", 403)(scope, receive, send)
            return
        result = asyncio.create_task(self._deliver(auth[0][7:], scope, receive, send))
        disconnect = asyncio.create_task(_disconnect(request))
        try:
            await asyncio.wait((result, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if result.done():
                result.result()
        finally:
            for task in (result, disconnect):
                if not task.done():
                    task.cancel()
            await asyncio.gather(result, disconnect, return_exceptions=True)

    async def _deliver(self, token: str, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def emit(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with self.service.admitted_read(token, self.operation) as (reading, deadline):
                body = {
                    "status": "ok",
                    "request_id": str(reading.request_id),
                    "fetched_at": reading.fetched_at,
                    "content_sha256": reading.content_sha256,
                    "document": asdict(reading.document) if reading.document else None,
                }
                response = JSONResponse(body, headers=_CACHE_HEADERS)
                if asyncio.get_running_loop().time() >= deadline:
                    raise ReadFailure("timeout")
                await response(scope, receive, emit)
        except ReadFailure as error:
            if not started:
                await _error(error.code, _STATUSES[error.code])(scope, receive, send)
        except Exception:  # noqa: BLE001 — adapter failure text can expose credential or document data
            if not started:
                await _error("unavailable", 503)(scope, receive, send)


def create_app(service: ReadService) -> Starlette:
    return Starlette(
        routes=[
            Route("/v1/live", _ReadRoute(service, "live"), methods=["POST"], name="live"),
            Route(
                "/v1/check-access",
                _ReadRoute(service, "check_access"),
                methods=["POST"],
                name="check_access",
            ),
        ]
    )
