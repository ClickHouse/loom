"""Resolve a worker read from current Loom authority, before and after I/O."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from server.knowledge.app.mcp_worker_read import WorkerReadRefused, WorkerReadScope
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
from server.knowledge.domain.mcp_worker_setup import WorkerMode
from server.knowledge.domain.mcp_worker_source import WorkerSourceRefused, decode_worker_source
from server.knowledge.domain.sources import KnowledgeSource


@dataclass(frozen=True, repr=False)
class RequestedResource:
    org: str
    namespace: str
    source_name: str
    resource_id: str


@dataclass(frozen=True, repr=False)
class CurrentConnection:
    org: str
    binding: ReaderConnection
    active: bool
    resource_space: str | None = None


@dataclass(frozen=True, repr=False)
class ReaderStanding:
    org: str
    subject: str
    grant_namespace: str


@dataclass(frozen=True, repr=False)
class ReaderEnrollment:
    org: str
    namespace: str
    source_id: UUID
    selection_generation: int
    requester: str
    connection_id: UUID
    connection_generation: int
    link_generation: int


GetStanding = Callable[[RequestedResource], Awaitable[ReaderStanding | None]]
GetSource = Callable[[str, str, str], Awaitable[KnowledgeSource | None]]
GetWorkerApproval = Callable[[str, str, UUID, str, WorkerMode, bool], Awaitable[int | None]]
GetConnection = Callable[[str, str, UUID, UUID], Awaitable[CurrentConnection | None]]
GetEnrollment = Callable[[str, str, UUID, str], Awaitable[ReaderEnrollment | None]]


@dataclass(frozen=True, repr=False)
class CurrentRead:
    request: RequestedResource
    standing: GetStanding
    source: GetSource
    connection: GetConnection
    enrollment: GetEnrollment
    worker_approval: GetWorkerApproval
    purpose: Literal["live", "stored"]

    async def resolve(self) -> WorkerReadScope:
        requested = self.request
        standing = await self.standing(requested)
        if (
            standing is None
            or standing.org != requested.org
            or standing.grant_namespace != requested.namespace
            or not standing.subject
            or standing.subject == "-"
        ):
            raise WorkerReadRefused("denied")
        source = await self.source(requested.org, requested.namespace, requested.source_name)
        if source is None or (source.org, source.namespace, source.name) != (
            requested.org,
            requested.namespace,
            requested.source_name,
        ):
            raise WorkerReadRefused("denied")
        try:
            config, selected = decode_worker_source(source)
        except WorkerSourceRefused:
            raise WorkerReadRefused("denied") from None
        if config.paused or not any(item.id == requested.resource_id for item in selected):
            raise WorkerReadRefused("denied")
        requester = standing.subject
        source_id = UUID(source.source_id)
        is_owner = requester == source.owner_subject
        if (not is_owner and config.reader_policy != "current_team_access") or (
            self.purpose == "stored" and config.mode == "live"
        ):
            raise WorkerReadRefused("denied")
        approval = await self.worker_approval(
            requested.org,
            requested.namespace,
            config.worker_id,
            config.profile_digest,
            "live" if self.purpose == "live" else config.mode,
            not is_owner,
        )
        if type(approval) is not int or approval != config.approval_generation:
            raise WorkerReadRefused("denied")

        async def current(owner: str, connection_id: UUID, generation: int) -> CurrentConnection:
            record = await self.connection(requested.org, owner, config.worker_id, connection_id)
            if (
                record is None
                or record.active is not True
                or record.org != requested.org
                or record.binding.owner != owner
                or record.binding.connection_id != connection_id
                or record.binding.connection_generation != generation
                or record.binding.worker_id != config.worker_id
                or record.binding.profile_digest != config.profile_digest
            ):
                raise WorkerReadRefused("reconnect")
            return record

        owner_account = await current(
            source.owner_subject, config.owner_connection_id, config.owner_connection_generation
        )
        reader = owner_account
        enrollment_generation = 0
        if not is_owner:
            enrollment = await self.enrollment(
                requested.org, requested.namespace, source_id, requester
            )
            if enrollment is None:
                raise WorkerReadRefused("reconnect")
            if (
                enrollment.org != requested.org
                or enrollment.namespace != requested.namespace
                or enrollment.source_id != source_id
                or enrollment.requester != requester
                or type(enrollment.link_generation) is not int
                or enrollment.link_generation <= 0
            ):
                raise WorkerReadRefused("denied")
            if enrollment.selection_generation != config.selection_generation:
                raise WorkerReadRefused("reconnect")
            enrollment_generation = enrollment.link_generation
            reader = await current(
                requester, enrollment.connection_id, enrollment.connection_generation
            )
            if not reader.resource_space or reader.resource_space != owner_account.resource_space:
                raise WorkerReadRefused("denied")
        return WorkerReadScope(
            worker_id=reader.binding.worker_id,
            org=requested.org,
            namespace=requested.namespace,
            source_id=source_id,
            selection_generation=config.selection_generation,
            requester=requester,
            connection_id=reader.binding.connection_id,
            connection_generation=reader.binding.connection_generation,
            profile_digest=reader.binding.profile_digest,
            approval_generation=approval,
            enrollment_generation=enrollment_generation,
            resource_id=requested.resource_id,
        )
