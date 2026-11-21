"""Keep promotion tied to the reviewed source through its shared writes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from server.core import lease_writes, pg
from server.core.errors import AuthInsufficientError
from server.core.repo_base import _default_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.proposal_review import pending_review
from server.memory.store import pg_memory, write_fence
from server.memory.store.rows import Identity, MemoryRow, identity_key


def require_reviewed(source: MemoryRow) -> None:
    if pending_review(source):
        raise AuthInsufficientError("An unreviewed proposal cannot be promoted")


@asynccontextmanager
async def publication(source: MemoryRow) -> AsyncIterator[Callable[[bool], None]]:
    import server.memory.store as memory_repo

    require_reviewed(source)
    ch = _default_client()
    async with lease_writes.fenced_many(
        [write_fence.lease_name(identity_key(source))],
        ch=write_fence._lease_client(ch),
        wait_s=write_fence._admission_wait_s(),
    ) as mark:
        if pg.enabled():
            async with pg_memory.locked_source(source):
                yield mark
        else:
            current = await memory_repo.get_by_key(
                Identity(
                    org=source.org,
                    namespace=source.namespace,
                    agent=source.agent,
                    user_id=source.user_id,
                    session_id=source.session_id,
                ),
                source.memory_key,
                scope=WHOLE_NAMESPACE,
                strong=True,
                fresh=True,
            )
            if not pg_memory.unchanged(source, current):
                raise pg_memory.AdmissionContendedError("The promotion's source changed")
            yield mark
