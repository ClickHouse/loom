"""Keep the model control plane reachable while data-plane work is draining."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from server.core.model_work_gate import ModelChangeInProgress, ModelWorkGate, get_model_work_gate


def _control_request(scope: Scope) -> bool:
    path = scope.get("path", "").rstrip("/")
    if path in ("/v1/health", "/v1/whoami", "/v1/auth/login", "/v1/admin/models"):
        return True
    if path in ("/v1/health/live", "/v1/health/ready"):
        return True
    if path.startswith(("/v1/admin/models/", "/oauth/", "/.well-known/")):
        return True
    # Dashboard page and asset loads: any GET outside the API and MCP roots.
    return scope.get("method") in ("GET", "HEAD") and not (
        path in ("/v1", "/mcp") or path.startswith(("/v1/", "/mcp/"))
    )


class ModelMaintenanceMiddleware:
    def __init__(self, app: ASGIApp, gate: ModelWorkGate | None = None) -> None:
        self.app = app
        self.gate = gate or get_model_work_gate()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or _control_request(scope):
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
            response = JSONResponse(
                {
                    "detail": {
                        "code": "MODEL_CHANGE_IN_PROGRESS",
                        "message": "Embedding configuration is being rebuilt. "
                        "Retry when setup is complete.",
                    }
                },
                status_code=503,
                headers={"Retry-After": "3", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
