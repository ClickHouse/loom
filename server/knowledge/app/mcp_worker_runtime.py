"""Start and stop the process-owned directory for provisioned source workers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from server.core import pg
from server.knowledge.app.mcp_worker_pg_directory import live_directory
from server.knowledge.app.mcp_worker_protected import LiveSourceDirectory
from server.knowledge.app.mcp_worker_route_config import load_worker_routes
from server.knowledge.app.mcp_worker_routes import WorkerRoutes, open_worker_routes


@dataclass(frozen=True, repr=False)
class _SourceWorkerRuntime:
    routes: WorkerRoutes
    directory: LiveSourceDirectory


_runtime: _SourceWorkerRuntime | None = None


def current_directory() -> LiveSourceDirectory | None:
    return _runtime.directory if pg.enabled() and _runtime is not None else None


def current_routes() -> WorkerRoutes | None:
    return _runtime.routes if pg.enabled() and _runtime is not None else None


@asynccontextmanager
async def source_worker_runtime(path: str) -> AsyncIterator[None]:
    global _runtime
    if not path or not pg.enabled():
        yield
        return
    async with open_worker_routes(load_worker_routes(Path(path))) as routes:
        if _runtime is not None:
            raise RuntimeError("source worker directory already running")
        _runtime = _SourceWorkerRuntime(routes, live_directory(routes))
        try:
            yield
        finally:
            _runtime = None
