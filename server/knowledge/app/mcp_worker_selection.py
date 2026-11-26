"""Verify access to every chosen identity without importing its content."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from server.core.diagnostics import private_work
from server.knowledge.app.mcp_worker_read import WorkerReadRefused, WorkerReadScope
from server.knowledge.domain.mcp_identity import ResourceIdentity, selection_sha256
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
from server.knowledge.domain.mcp_worker_setup import (
    VerifiedSelectionGrant,
    WorkerMode,
    WorkerSetupRejected,
    checked_worker_selection,
)
from server.settings import settings

if TYPE_CHECKING:
    from server.knowledge.app.mcp_worker_read import WorkerReadChannel

# not a setting: a selection proof can authorize setup only in its issuance window
_EVIDENCE_SECONDS = 30


@dataclass(frozen=True, repr=False)
class SelectionAuthority:
    org: str
    namespace: str
    destination_id: UUID
    source_id: UUID
    selection_generation: int
    approval_generation: int
    connection: ReaderConnection

    def scope(self, item: ResourceIdentity) -> WorkerReadScope:
        account = self.connection
        return WorkerReadScope(
            worker_id=account.worker_id,
            org=self.org,
            namespace=self.namespace,
            source_id=self.source_id,
            selection_generation=self.selection_generation,
            requester=account.owner,
            connection_id=account.connection_id,
            connection_generation=account.connection_generation,
            profile_digest=account.profile_digest,
            approval_generation=self.approval_generation,
            resource_id=item.id,
        )


async def verify_worker_selection(
    authority: SelectionAuthority,
    items: tuple[ResourceIdentity, ...],
    *,
    mode: WorkerMode,
    channel: WorkerReadChannel,
    current: Callable[[], Awaitable[SelectionAuthority | None]],
    deadline: float,
) -> VerifiedSelectionGrant:
    try:
        items = checked_worker_selection(items, mode)
    except WorkerSetupRejected:
        raise WorkerReadRefused("denied") from None
    started = int(time.time())
    with private_work():
        try:
            async with asyncio.timeout_at(deadline):
                confirmed = await _check_all(authority, items, channel, current)
        except TimeoutError:
            raise WorkerReadRefused("timeout") from None
    if confirmed >= started + _EVIDENCE_SECONDS:
        raise WorkerReadRefused("timeout")
    return VerifiedSelectionGrant(
        authority.org,
        authority.connection,
        authority.approval_generation,
        selection_sha256(items),
        confirmed,
        started + _EVIDENCE_SECONDS,
        authority.namespace,
        authority.destination_id,
        authority.source_id,
        authority.selection_generation,
    )


async def _check_all(
    expected: SelectionAuthority,
    items: tuple[ResourceIdentity, ...],
    channel: WorkerReadChannel,
    current: Callable[[], Awaitable[SelectionAuthority | None]],
) -> int:
    async def verify(item: ResourceIdentity) -> None:
        scope = expected.scope(item)

        async def require_current() -> WorkerReadScope:
            if await current() != expected:
                raise WorkerReadRefused("denied")
            return scope

        await channel.run(scope, "check_access", require_current=require_current)

    parallelism = settings.knowledge_source_worker_selection_parallelism
    for start in range(0, len(items), parallelism):
        pending = [asyncio.create_task(verify(item)) for item in items[start : start + parallelism]]
        try:
            await asyncio.gather(*pending)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    if await current() != expected:
        raise WorkerReadRefused("denied")
    return int(time.time())
