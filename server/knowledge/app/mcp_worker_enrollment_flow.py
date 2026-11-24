"""Link a worker account under current Loom ownership and approval exactly once."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from server.core.diagnostics import private_work
from server.knowledge.app.mcp_worker_enrollment import (
    EnrollmentChallenge,
    EnrollmentRefused,
    enroll_worker_connection,
)
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
from server.knowledge.store import pg_mcp_worker_enrollment as enrollment

if TYPE_CHECKING:
    from asyncpg import Connection
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )

    from server.knowledge.app.mcp_worker_authority import CurrentConnection
    from server.knowledge.app.mcp_worker_read import WorkerReadChannel


@dataclass(frozen=True, repr=False)
class EnrollmentRoute:
    """The admin's current approval and the deployment's registered channel."""

    approval_org: str
    approval_namespace: str
    worker_id: UUID
    profile_digest: str
    approval_generation: int
    issuer: str
    grant_key: Ed25519PrivateKey
    worker_key: Ed25519PublicKey
    channel: WorkerReadChannel


@dataclass(frozen=True, repr=False)
class EnrollmentAuthority:
    """Server-resolved identity and owner scope; none originates from submitted wire fields."""

    org: str
    owner_namespace: str
    owner: str
    origin: enrollment.WriteOrigin
    route: EnrollmentRoute
    # Obtain current actor, membership, worker approval and erasure standing. On
    # writes, use the supplied transaction and participate in its authority fences.
    current_generation: Callable[[Connection | None], Awaitable[int | None]]

    async def is_current(self, conn: Connection | None) -> bool:
        current = await self.current_generation(conn)
        return type(current) is int and current == self.route.approval_generation


async def link_worker_account(
    authority: EnrollmentAuthority, *, connection_id: UUID, generation: int, deadline: float
) -> CurrentConnection:
    with private_work():
        try:
            async with asyncio.timeout_at(deadline):
                return await _link(
                    authority, connection_id=connection_id, generation=generation, deadline=deadline
                )
        except TimeoutError:
            raise EnrollmentRefused() from None


def _challenge(
    authority: EnrollmentAuthority, connection_id: UUID, generation: int
) -> tuple[enrollment.Challenge, EnrollmentChallenge]:
    route = authority.route
    if (
        authority.org != route.approval_org
        or not authority.owner_namespace
        or not route.approval_namespace
        or not authority.owner
        or authority.owner == "-"
        or type(route.approval_generation) is not int
        or route.approval_generation <= 0
        or route.worker_id.int == 0
        or connection_id.int == 0
        or type(generation) is not int
        or not 0 < generation < 2**63
    ):
        raise EnrollmentRefused()
    account = enrollment.AccountKey(
        authority.org,
        authority.owner_namespace,
        authority.owner,
        route.worker_id,
        route.profile_digest,
        connection_id,
    )
    now = int(time.time())
    challenge = enrollment.Challenge(account, generation, uuid4(), now, now + 30)
    wire = EnrollmentChallenge(
        org=account.org,
        requester=account.requester,
        worker_id=account.worker_id,
        profile_digest=account.profile_digest,
        nonce=challenge.nonce,
        issued_at=challenge.issued_at,
        expires_at=challenge.expires_at,
        audience=f"urn:loom:source-worker:{account.worker_id}:enrollment",
        connection_id=account.connection_id,
        connection_generation=challenge.generation,
    )

    return challenge, wire


async def _link(
    authority: EnrollmentAuthority, *, connection_id: UUID, generation: int, deadline: float
) -> CurrentConnection:
    route = authority.route
    challenge, wire = _challenge(authority, connection_id, generation)
    account = challenge.account
    async with enrollment.owner_transaction(account) as conn:
        if not await authority.is_current(conn) or not await enrollment.issue_challenge(
            conn, challenge, authority.origin
        ):
            raise EnrollmentRefused()

    async def current() -> EnrollmentChallenge | None:
        return wire if await authority.is_current(None) else None

    async def consume(expected: EnrollmentChallenge, proposed: CurrentConnection) -> None:
        if (
            expected != wire
            or proposed.org != account.org
            or proposed.binding
            != ReaderConnection(
                account.requester,
                account.worker_id,
                account.connection_id,
                generation,
                account.profile_digest,
            )
        ):
            raise EnrollmentRefused()
        async with enrollment.owner_transaction(account) as conn:
            if not await authority.is_current(conn):
                raise EnrollmentRefused()
            accepted = await enrollment.consume_challenge(
                conn, challenge, resource_space=proposed.resource_space
            )
        if not accepted:
            raise EnrollmentRefused()

    return await enroll_worker_connection(
        wire,
        issuer=route.issuer,
        key=route.grant_key,
        verifier=route.worker_key,
        require_current=current,
        request_statement=route.channel.request_enrollment,
        consume=consume,
        deadline=deadline,
    )
