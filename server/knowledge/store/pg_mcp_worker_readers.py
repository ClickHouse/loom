"""Keep each team reader's explicit enrollment tied to one source selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.core import pg
from server.knowledge.store.pg_mcp_worker_enrollment import WriteOrigin

if TYPE_CHECKING:
    from uuid import UUID

    from asyncpg import Connection


@dataclass(frozen=True, repr=False)
class LinkKey:
    org: str
    namespace: str
    source_id: UUID
    requester: str

    def parameters(self) -> tuple[str, str, UUID, str]:
        return self.org, self.namespace, self.source_id, self.requester


@dataclass(frozen=True, repr=False)
class Link:
    selection_generation: int
    connection_id: UUID
    connection_generation: int
    link_generation: int
    active: bool


_SCOPE = "org = $1 AND namespace = $2 AND source_id = $3 AND user_id = $4"
_GET = f"""
SELECT selection_generation, connection_id, connection_generation, link_generation, active
FROM loom.mcp_worker_source_readers WHERE {_SCOPE}
"""
_INSERT = """
INSERT INTO loom.mcp_worker_source_readers
    (org, namespace, source_id, user_id, selection_generation, connection_id,
     connection_generation, agent, session_id, actor, source_class, active, link_generation)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, true,
        nextval('loom.mcp_worker_source_reader_generations'))
ON CONFLICT (org, namespace, source_id, user_id) DO NOTHING
RETURNING link_generation
"""
_REPLACE = f"""
UPDATE loom.mcp_worker_source_readers
SET selection_generation = $5, connection_id = $6, connection_generation = $7,
    agent = $8, session_id = $9, actor = $10, source_class = $11, active = true,
    link_generation = nextval('loom.mcp_worker_source_reader_generations')
WHERE {_SCOPE} AND link_generation = $12 AND selection_generation <= $5
RETURNING link_generation
"""
_DISCONNECT = f"""
UPDATE loom.mcp_worker_source_readers
SET active = false, link_generation = nextval('loom.mcp_worker_source_reader_generations')
WHERE {_SCOPE} AND link_generation = $5 AND active
RETURNING link_generation
"""


async def current(key: LinkKey) -> Link | None:
    if not pg.enabled():
        return None
    async with pg.connection() as conn:
        return await current_on(conn, key)


async def current_on(conn: Connection, key: LinkKey, *, lock: bool = False) -> Link | None:
    if not pg.enabled():
        return None
    if lock and not conn.is_in_transaction():
        raise RuntimeError("source reader lock requires an authority transaction")
    query = _GET + " FOR SHARE NOWAIT" if lock else _GET
    row = await conn.fetchrow(query, *key.parameters())
    if row is None:
        return None
    return Link(
        row["selection_generation"],
        row["connection_id"],
        row["connection_generation"],
        row["link_generation"],
        row["active"],
    )


async def set_on(
    conn: Connection,
    key: LinkKey,
    *,
    selection_generation: int,
    connection_id: UUID,
    connection_generation: int,
    expected_link_generation: int,
    origin: WriteOrigin,
) -> int | None:
    """The caller holds the current source, worker profile, and account authorizations."""
    if not pg.enabled() or not conn.is_in_transaction():
        raise RuntimeError("source reader link requires an authority transaction")
    generations = (selection_generation, connection_generation)
    if not (
        all(type(value) is int and 0 < value < 2**63 for value in generations)
        and type(expected_link_generation) is int
        and 0 <= expected_link_generation < 2**63
        and connection_id.int != 0
    ):
        return None
    args = (
        *key.parameters(),
        selection_generation,
        connection_id,
        connection_generation,
        origin.agent,
        origin.session_id,
        origin.actor,
        origin.source_class,
    )
    if expected_link_generation == 0:
        generation = await conn.fetchval(_INSERT, *args)
    else:
        generation = await conn.fetchval(_REPLACE, *args, expected_link_generation)
    return int(generation) if type(generation) is int and generation > 0 else None


async def revoke_on(conn: Connection, key: LinkKey, *, expected_link_generation: int) -> bool:
    if not pg.enabled() or not conn.is_in_transaction():
        raise RuntimeError("source reader unlink requires an authority transaction")
    if type(expected_link_generation) is not int or not 0 < expected_link_generation < 2**63:
        return False
    generation = await conn.fetchval(_DISCONNECT, *key.parameters(), expected_link_generation)
    return type(generation) is int
