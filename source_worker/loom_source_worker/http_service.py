"""Private HTTP callback for Loom's transient reads."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

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


def create_app(service: ReadService) -> Starlette:
    async def invoke(request: Request, operation: Literal["live", "check_access"]) -> Response:
        if (
            request.url.query
            or request.headers.get("content-length", "0") != "0"
            or "transfer-encoding" in request.headers
        ):
            return JSONResponse({"status": "denied"}, 400, headers=_CACHE_HEADERS)
        auth = request.headers.getlist("authorization")
        if len(auth) != 1 or not auth[0].startswith("Bearer "):
            return JSONResponse({"status": "denied"}, 403, headers=_CACHE_HEADERS)
        result = asyncio.create_task(service.execute(auth[0][7:], operation))
        disconnect = asyncio.create_task(_disconnect(request))
        try:
            await asyncio.wait((result, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect.done():
                return Response(status_code=499, headers=_CACHE_HEADERS)
            reading = result.result()
            response = {
                "status": "ok",
                "request_id": str(reading.request_id),
                "fetched_at": reading.fetched_at,
                "content_sha256": reading.content_sha256,
                "document": asdict(reading.document) if reading.document else None,
            }
            return JSONResponse(response, headers=_CACHE_HEADERS)
        except ReadFailure as error:
            return JSONResponse(
                {"status": error.code}, _STATUSES[error.code], headers=_CACHE_HEADERS
            )
        except Exception:  # noqa: BLE001 — adapter failure text can expose credential or document data
            return JSONResponse({"status": "unavailable"}, 503, headers=_CACHE_HEADERS)
        finally:
            for task in (result, disconnect):
                if not task.done():
                    task.cancel()
            await asyncio.gather(result, disconnect, return_exceptions=True)

    async def live(request: Request) -> Response:
        return await invoke(request, "live")

    async def check_access(request: Request) -> Response:
        return await invoke(request, "check_access")

    return Starlette(
        routes=[
            Route("/v1/live", live, methods=["POST"]),
            Route("/v1/check-access", check_access, methods=["POST"]),
        ]
    )
