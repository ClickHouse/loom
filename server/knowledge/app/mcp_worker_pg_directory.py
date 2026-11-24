"""Read opaque source worker authority from the deployment's Postgres store."""

import asyncio
from uuid import UUID

from server.knowledge.app.mcp_worker_authority import CurrentConnection, ReaderEnrollment
from server.knowledge.app.mcp_worker_protected import Deliver, LiveSourceDirectory
from server.knowledge.app.mcp_worker_read import WorkerReadRefused, WorkerReadScope
from server.knowledge.app.mcp_worker_routes import WorkerRoutes
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
from server.knowledge.domain.mcp_worker_setup import WorkerMode
from server.knowledge.store import pg_mcp_worker_approvals as approvals
from server.knowledge.store import pg_mcp_worker_enrollment as accounts
from server.knowledge.store import pg_mcp_worker_readers as readers
from server.knowledge.store.sources import get_source


async def current_connection(
    org: str, owner: str, worker_id: UUID, connection_id: UUID
) -> CurrentConnection | None:
    row = await accounts.current_reader_account(org, owner, worker_id, connection_id)
    if row is None:
        return None
    profile_digest, state = row
    binding = ReaderConnection(owner, worker_id, connection_id, state.generation, profile_digest)
    return CurrentConnection(org, binding, state.active, state.resource_space)


async def current_enrollment(
    org: str, namespace: str, source_id: UUID, requester: str
) -> ReaderEnrollment | None:
    row = await readers.current(readers.LinkKey(org, namespace, source_id, requester))
    if row is None or row.active is not True:
        return None
    return ReaderEnrollment(
        org,
        namespace,
        source_id,
        row.selection_generation,
        requester,
        row.connection_id,
        row.connection_generation,
        row.link_generation,
    )


async def fence_live_delivery(scope: WorkerReadScope, deliver: Deliver, deadline: float) -> None:
    """Keep revocation and erasure fenced until this frame has been handed off."""
    owner = scope.source_owner
    if (
        owner is None
        or owner.worker_id != scope.worker_id
        or owner.profile_digest != scope.profile_digest
    ):
        raise WorkerReadRefused("denied")
    key = approvals.ApprovalKey(scope.org, scope.namespace, scope.worker_id, scope.profile_digest)
    end = min(deadline, asyncio.get_running_loop().time() + 1)
    async with asyncio.timeout_at(end), approvals.scope_transaction(key) as conn:
        approval = await approvals.current_on(conn, key, lock=True)
        team = owner.owner != scope.requester
        if (
            approval is None
            or not approval.active
            or not approval.modes & approvals.LIVE
            or approval.generation != scope.approval_generation
            or (team and not approval.team_access)
        ):
            raise WorkerReadRefused("denied")
        if team:
            link_key = readers.LinkKey(scope.org, scope.namespace, scope.source_id, scope.requester)
            link = await readers.current_on(conn, link_key, lock=True)
            if link != readers.Link(
                scope.selection_generation,
                scope.connection_id,
                scope.connection_generation,
                scope.enrollment_generation,
                True,
            ):
                raise WorkerReadRefused("denied")
        elif (scope.connection_id, scope.connection_generation, scope.enrollment_generation) != (
            owner.connection_id,
            owner.connection_generation,
            0,
        ):
            raise WorkerReadRefused("denied")

        async def account_space(subject: str, connection: UUID, generation: int) -> str | None:
            row = await accounts.reader_account_on(
                conn, scope.org, subject, scope.worker_id, connection, lock=True
            )
            if row is None:
                raise WorkerReadRefused("denied")
            profile, account = row
            if (
                profile != scope.profile_digest
                or not account.active
                or account.generation != generation
            ):
                raise WorkerReadRefused("denied")
            return account.resource_space

        space = await account_space(owner.owner, owner.connection_id, owner.connection_generation)
        if team and (
            not space
            or space
            != await account_space(
                scope.requester, scope.connection_id, scope.connection_generation
            )
        ):
            raise WorkerReadRefused("denied")
        if asyncio.get_running_loop().time() >= end:
            raise WorkerReadRefused("denied")
        await deliver()


def live_directory(routes: WorkerRoutes) -> LiveSourceDirectory:
    """Compose the canonical source and account stores with a provisioned route registry."""

    async def approval(
        org: str, namespace: str, worker: UUID, profile: str, mode: WorkerMode, team: bool
    ) -> int | None:
        if not routes.permits(worker, org, profile, mode, team):
            return None
        return await approvals.current_generation(org, namespace, worker, profile, mode, team)

    return LiveSourceDirectory(
        source=get_source,
        connection=current_connection,
        enrollment=current_enrollment,
        worker=routes.get,
        worker_approval=approval,
        fence_delivery=fence_live_delivery,
    )
