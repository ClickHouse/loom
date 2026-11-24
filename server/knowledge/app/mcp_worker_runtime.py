"""Start and stop the process-owned directory for provisioned source workers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from server.knowledge.app.mcp_worker_pg_directory import live_directory
from server.knowledge.app.mcp_worker_protected import LiveSourceDirectory
from server.knowledge.app.mcp_worker_route_config import load_worker_routes
from server.knowledge.app.mcp_worker_routes import open_worker_routes

_directory: LiveSourceDirectory | None = None


def current_directory() -> LiveSourceDirectory | None:
    return _directory


@asynccontextmanager
async def source_worker_runtime(path: str) -> AsyncIterator[None]:
    global _directory
    if not path:
        yield
        return
    async with open_worker_routes(load_worker_routes(Path(path))) as routes:
        if _directory is not None:
            raise RuntimeError("source worker directory already running")
        _directory = live_directory(routes)
        try:
            yield
        finally:
            _directory = None
