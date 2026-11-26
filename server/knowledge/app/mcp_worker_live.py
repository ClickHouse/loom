"""Transient selected reads and their publication check."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from server.core.diagnostics import private_work
from server.knowledge.app.mcp_worker_read import (
    WorkerReadChannel,
    WorkerReadRefused,
    WorkerReadScope,
)
from server.knowledge.domain.mcp_identity import resource_uri

ResolveScope = Callable[[], Awaitable[WorkerReadScope]]
ResolveWorker = Callable[[UUID], WorkerReadChannel | None]
RetainPublication = Callable[
    [WorkerReadScope, Callable[[], Awaitable[None]], float], Awaitable[None]
]


@dataclass(frozen=True, repr=False)
class LiveDocument:
    source_id: UUID
    resource_id: str
    uri: str
    source_link: str | None
    fetched_at: int
    title: str
    text: str
    mime_type: str


def _display_link(uri: str | None) -> str | None:
    if not uri or "\\" in uri or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri):
        return None
    try:
        parsed = urlsplit(uri)
        if parsed.scheme == "https" and parsed.hostname and parsed.username is None:
            return uri
    except ValueError:
        pass
    return None


async def read_selected_live(
    *,
    resolve: ResolveScope,
    worker: ResolveWorker,
    retain: RetainPublication,
    deadline: float,
) -> LiveDocument:
    """Return to the protected transport. The caller installs its publication barrier."""
    try:
        with private_work():
            async with asyncio.timeout_at(deadline):
                scope = await resolve()
                channel = worker(scope.worker_id)
                if channel is None:
                    raise WorkerReadRefused("unavailable")
                read = await channel.run(scope, "live", require_current=resolve)
                if read.document is None:
                    raise WorkerReadRefused("denied")

                async def validate() -> None:
                    if await resolve() != scope or worker(scope.worker_id) is not channel:
                        raise WorkerReadRefused("denied")

                await validate()
            # The protected publisher owns a separate deadline for validation and delivery.
            await retain(scope, validate, deadline)
            return LiveDocument(
                scope.source_id,
                scope.resource_id,
                resource_uri(str(scope.source_id), scope.resource_id),
                _display_link(read.document.source_link),
                read.fetched_at,
                read.document.title,
                read.document.text,
                read.document.mime_type,
            )
    except TimeoutError:
        raise WorkerReadRefused("timeout") from None
