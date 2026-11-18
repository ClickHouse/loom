"""Own HTTP privacy through the complete instrumented application call."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from server.auth.session_protection import request_declaration
from server.core import request_work
from server.core.request_errors import MarkedRequestError, mark_preserved_error, preserve_error


class HTTPApplication(FastAPI):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await super().__call__(scope, receive, send)
            return
        with request_work.http_owner(scope), request_declaration(scope):
            try:
                await super().__call__(scope, receive, send)
            except BaseException as error:
                if not request_work.http_mirror_marked():
                    raise
                if preserve_error(error):
                    mark_preserved_error(error)
                    raise
                raise MarkedRequestError() from None
