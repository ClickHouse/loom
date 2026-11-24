"""Read opaque source worker authority from the deployment's Postgres store."""

from uuid import UUID

from server.knowledge.app.mcp_worker_authority import CurrentConnection, ReaderEnrollment
from server.knowledge.app.mcp_worker_protected import LiveSourceDirectory
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
    )
