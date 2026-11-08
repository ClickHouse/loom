"""Keep the model control plane reachable while data-plane work is draining,
and refuse work on a process whose configuration drifted from the deployment's."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from server.core import deployment_config
from server.core.errors import classified
from server.core.model_work_gate import ModelChangeInProgress, ModelWorkGate, get_model_work_gate


def _control_request(scope: Scope) -> bool:
    path = scope.get("path", "").rstrip("/")
    if path in ("/v1/health", "/v1/whoami", "/v1/auth/login", "/v1/admin/models"):
        return True
    if path in ("/v1/health/live", "/v1/health/ready"):
        return True
    # The JWKS too: a gateway verifying the identity token of the model calls a
    # model change makes reads it there.
    if path.startswith(("/v1/admin/models/", "/oauth/", "/.well-known/", "/v1/.well-known/")):
        return True
    # Dashboard page and asset loads: any GET outside the API and MCP roots.
    return scope.get("method") in ("GET", "HEAD") and not (
        path in ("/v1", "/mcp") or path.startswith(("/v1/", "/mcp/"))
    )


def _probe_or_page(scope: Scope) -> bool:
    path = scope.get("path", "").rstrip("/")
    if path in ("/v1/health", "/v1/health/live", "/v1/health/ready"):
        return True
    return scope.get("method") in ("GET", "HEAD") and not (
        path in ("/v1", "/mcp", "/oauth") or path.startswith(("/v1/", "/mcp/", "/oauth/"))
    )


def _unavailable(*, code: str, message: str, headers: dict[str, str]) -> JSONResponse:
    """A 503 in the envelope every other error has (this middleware runs outside
    the exception handlers)."""
    detail = classified({"code": code, "message": message}, 503)
    return JSONResponse({"detail": detail}, status_code=503, headers=headers)


async def _refuse_drifted(scope: Scope, receive: Receive, send: Send) -> None:
    """Not-ready stops new connections only. A drifted process would keep
    serving the open ones in another embedding space or under another root
    secret, so it answers 503 and closes each until it is replaced."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1013})
        return
    response = _unavailable(
        code="CONFIG_DRIFT",
        message="This server's configuration differs from the deployment's. "
        "Retry: another server answers.",
        headers={"Retry-After": "1", "Cache-Control": "no-store", "Connection": "close"},
    )
    await response(scope, receive, send)


async def _refuse_refused_configuration(scope: Scope, receive: Receive, send: Send) -> None:
    """Held for a configuration the gateway check refused: nothing is
    rebuilding and only an operator ends it, so no Retry-After, and a code
    the SDKs never retry. The reason is in /v1/health, for the operator."""
    response = _unavailable(
        code="GATEWAY_REQUIRED",
        message="This server refuses model work: the model configuration saved in "
        "the dashboard sends model calls off the inference gateway "
        "(/v1/health: model_configuration_refused). Save one at a host in "
        "LOOM_INFERENCE_GATEWAY_HOSTS, or reset it to the environment's.",
        headers={"Cache-Control": "no-store"},
    )
    await response(scope, receive, send)


class ModelMaintenanceMiddleware:
    def __init__(self, app: ASGIApp, gate: ModelWorkGate | None = None) -> None:
        self.app = app
        self.gate = gate or get_model_work_gate()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if deployment_config.drift() and not _probe_or_page(scope):
            await _refuse_drifted(scope, receive, send)
            return
        if _control_request(scope):
            await self.app(scope, receive, send)
            return
        try:
            async with self.gate.admit():
                # Keep admission through response/background-task completion,
                # not just until a streaming response's headers are available.
                await self.app(scope, receive, send)
        except ModelChangeInProgress:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
                return
            if self.gate.refusal:
                await _refuse_refused_configuration(scope, receive, send)
                return
            response = _unavailable(
                code="MODEL_CHANGE_IN_PROGRESS",
                message="Embedding configuration is being rebuilt. Retry when setup is complete.",
                headers={"Retry-After": "3", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
