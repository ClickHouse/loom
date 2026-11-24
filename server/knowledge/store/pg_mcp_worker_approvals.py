"""Store approvals of opaque worker profiles on one collection scope."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.core import pg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from asyncpg import Connection, Record

    from server.knowledge.domain.mcp_worker_setup import WorkerMode

from server.knowledge.store.pg_mcp_worker_enrollment import WriteOrigin

LIVE = 1
SNAPSHOT = 2
REFRESH = 4
_ALL_MODES = LIVE | SNAPSHOT | REFRESH


@dataclass(frozen=True, repr=False)
class ApprovalKey:
    org: str
    namespace: str
    worker_id: UUID
    profile_digest: str

    def arguments(self) -> tuple[str, str, UUID, str]:
        return self.org, self.namespace, self.worker_id, self.profile_digest


@dataclass(frozen=True, repr=False)
class ApprovalOwner:
    user_id: str
    origin: WriteOrigin


@dataclass(frozen=True, repr=False)
class WorkerApproval:
    generation: int
    modes: int
    team_access: bool
    active: bool


_SCOPE = "org = $1 AND namespace = $2 AND worker_id = $3 AND profile_digest = $4"
_FIELDS = "generation, modes, team_access, active"
_CURRENT = f"SELECT {_FIELDS} FROM loom.mcp_worker_approvals WHERE {_SCOPE}"
_CREATE = """
INSERT INTO loom.mcp_worker_approvals
    (org, namespace, worker_id, profile_digest, modes, team_access,
     user_id, agent, session_id, actor, source_class, active, generation)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, true,
        nextval('loom.mcp_worker_approval_generations'))
ON CONFLICT (org, namespace, worker_id, profile_digest) DO NOTHING
RETURNING generation, modes, team_access, active
"""
_REPLACE = f"""
UPDATE loom.mcp_worker_approvals
SET modes = $5, team_access = $6, user_id = $7, agent = $8, session_id = $9,
    actor = $10, source_class = $11, active = ($5 > 0),
    generation = nextval('loom.mcp_worker_approval_generations')
WHERE {_SCOPE} AND generation = $12
RETURNING {_FIELDS}
"""


def _result(row: Record | None) -> WorkerApproval | None:
    if row is None:
        return None
    return WorkerApproval(row["generation"], row["modes"], row["team_access"], row["active"])


async def current_on(
    conn: Connection, key: ApprovalKey, *, lock: bool = False
) -> WorkerApproval | None:
    """The caller uses a transaction for a read it will rely on during a write."""
    if not pg.enabled():
        return None
    if lock and not conn.is_in_transaction():
        raise RuntimeError("worker approval lock requires an authority transaction")
    query = _CURRENT + " FOR SHARE" if lock else _CURRENT
    return _result(await conn.fetchrow(query, *key.arguments()))


async def current_generation(
    org: str, namespace: str, worker_id: UUID, digest: str, mode: WorkerMode, team_reader: bool
) -> int | None:
    if not pg.enabled():
        return None
    async with pg.connection() as conn:
        approval = await current_on(conn, ApprovalKey(org, namespace, worker_id, digest))
    required = {"live": LIVE, "snapshot": SNAPSHOT, "selected_refresh": REFRESH}.get(mode, 0)
    if (
        approval is None
        or not approval.active
        or not (approval.modes & required)
        or (team_reader and not approval.team_access)
    ):
        return None
    return approval.generation


async def replace_on(
    conn: Connection,
    key: ApprovalKey,
    *,
    expected_generation: int,
    modes: int,
    team_access: bool,
    owner: ApprovalOwner,
) -> WorkerApproval | None:
    """Change only the exact approval the authorized admin observed."""
    if not pg.enabled() or not conn.is_in_transaction():
        raise RuntimeError("worker approval requires an authority transaction")
    if (
        type(modes) is not int
        or not 0 <= modes <= _ALL_MODES
        or type(expected_generation) is not int
        or expected_generation < 0
        or (expected_generation == 0 and modes == 0)
        or type(team_access) is not bool
        or (not modes and team_access)
    ):
        return None
    args = (
        *key.arguments(),
        modes,
        team_access,
        owner.user_id,
        owner.origin.agent,
        owner.origin.session_id,
        owner.origin.actor,
        owner.origin.source_class,
    )
    if expected_generation == 0:
        return _result(await conn.fetchrow(_CREATE, *args))
    return _result(await conn.fetchrow(_REPLACE, *args, expected_generation))


@asynccontextmanager
async def scope_transaction(key: ApprovalKey) -> AsyncIterator[Connection]:
    if not pg.enabled():
        raise RuntimeError("worker approval requires Postgres authority")
    async with pg.transaction() as conn:
        await conn.execute(
            f"SELECT pg_advisory_xact_lock_shared({pg.KNOWLEDGE_LOCK_KEY})", key.org, key.namespace
        )
        yield conn
