"""Apply the Loom collection and session publication gates to a live source read."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from server.auth import authz
from server.auth.answer_tokens import is_answer_token
from server.auth.context import current_auth
from server.auth.jwt import AuthInfo
from server.auth.session_protection import protected_for
from server.core import request_tail, request_work
from server.core.ch import sequential_reads
from server.core.diagnostics import private_work
from server.identity.domain.read_scope import effective_read_scope
from server.knowledge.app import connected_accounts, session_admission
from server.knowledge.app.mcp_worker_authority import (
    CurrentRead,
    GetConnection,
    GetEnrollment,
    GetSource,
    GetWorkerApproval,
    ReaderStanding,
    RequestedResource,
)
from server.knowledge.app.mcp_worker_live import LiveDocument, ResolveWorker, read_selected_live
from server.knowledge.app.mcp_worker_read import WorkerReadRefused, WorkerReadScope
from server.operations.base import Role
from server.settings import settings

Deliver = Callable[[], Awaitable[None]]
FenceDelivery = Callable[[WorkerReadScope, Deliver, float], Awaitable[None]]
_PUBLICATION_BUDGET_SECONDS = 2


@dataclass(frozen=True, repr=False)
class LiveSourceDirectory:
    source: GetSource
    connection: GetConnection
    enrollment: GetEnrollment
    worker: ResolveWorker
    worker_approval: GetWorkerApproval
    fence_delivery: FenceDelivery | None = None


def _require_context(org: str, auth: AuthInfo) -> None:
    if (
        current_auth() is not auth
        or auth.org != org
        or not settings.auth_actually_required()
        or not settings.knowledge_mirrored_sources_enabled
        or not protected_for(auth)
        or is_answer_token(auth)
        or effective_read_scope(None, auth) != "namespace"
        or bool(auth.read_user_id)
    ):
        raise WorkerReadRefused("denied")


async def _retain(
    auth: AuthInfo, check: Deliver, fence: request_tail.PublicationFence, deadline: float
) -> None:
    loop = asyncio.get_running_loop()
    checked = False

    async def validate() -> None:
        nonlocal checked
        checked = False
        with private_work():
            async with asyncio.timeout_at(deadline):
                _require_context(auth.org, auth)
                await session_admission.require(auth)
                await check()
                _require_context(auth.org, auth)
                checked = True

    def fresh() -> bool:
        return checked and loop.time() < deadline

    await validate()
    if not fresh() or not request_tail.retain_publication(validate, validate, fresh, fence=fence):
        raise WorkerReadRefused("denied")


async def protected_live_read(
    request: RequestedResource, directory: LiveSourceDirectory, *, deadline: float
) -> LiveDocument:
    with private_work():
        auth = current_auth()
        if auth is None or not request_tail.required_active():
            raise WorkerReadRefused("denied")
        _require_context(request.org, auth)
        fence_delivery = directory.fence_delivery
        if fence_delivery is None:
            raise WorkerReadRefused("unavailable")
        if not request_work.mark_mirrored():
            raise WorkerReadRefused("denied")

        async def standing(target: RequestedResource) -> ReaderStanding | None:
            _require_context(target.org, auth)
            await session_admission.require(auth)
            if not await authz.row_in_scope(target.org, target.namespace, Role.VIEWER):
                return None
            org, subject = await connected_accounts.own_subject(auth)
            return ReaderStanding(org, subject, target.namespace)

        current = CurrentRead(
            request,
            standing,
            directory.source,
            directory.connection,
            directory.enrollment,
            directory.worker_approval,
            "live",
        )

        async def retain(scope: WorkerReadScope, check: Deliver, read_until: float) -> None:
            # A completed worker read gets its own bounded time for accounting and send.
            loop = asyncio.get_running_loop()
            retain_until = min(read_until, loop.time()) + _PUBLICATION_BUDGET_SECONDS

            async def fence(deliver: Deliver) -> None:
                _require_context(scope.org, auth)
                from server.knowledge.app.sources import sources_lease

                # Source retirement holds this lease. Take it before the Postgres
                # fence so the source cannot change between validation and handoff.
                with private_work():
                    end = min(retain_until, loop.time() + 1)
                    async with (
                        asyncio.timeout_at(end),
                        sources_lease(scope.org, scope.namespace, wait_s=0),
                    ):
                        with sequential_reads():
                            await check()
                        await fence_delivery(scope, deliver, end)

            await _retain(auth, check, fence, retain_until)

        return await read_selected_live(
            resolve=current.resolve, worker=directory.worker, retain=retain, deadline=deadline
        )
