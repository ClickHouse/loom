"""Outer admission includes request-tail settlement after inner dispatch ends."""

from starlette.types import ASGIApp, Receive, Scope, Send

from server.core import retirement
from server.transport.model_maintenance import _refuse


class RetirementMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        try:
            async with retirement.get_gate().admit():
                await self.app(scope, receive, send)
        except retirement.ProcessStopping:
            await _refuse(
                scope,
                receive,
                send,
                code="PROCESS_STOPPING",
                message="This server is stopping. Retry another server.",
                retry_after="1",
            )
