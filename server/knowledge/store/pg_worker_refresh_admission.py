"""Serialize selected refresh admission and bound queue metadata per source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.core import pg
from server.knowledge.domain.mcp_worker_refresh import MAX_DOCUMENTS_PER_JOB, RefreshRejected

if TYPE_CHECKING:
    from uuid import UUID

    from asyncpg import Connection

MAX_RETAINED = 2 * MAX_DOCUMENTS_PER_JOB
_SCOPE = "org = $1 AND namespace = $2 AND admission_source_id = $3"
_OPEN = """
INSERT INTO loom.mcp_worker_refresh_admission
    (org, namespace, source_id, agent, user_id, session_id, actor, source_class)
VALUES ($1, $2, $3, $4, $5, $6, $7, 'external')
ON CONFLICT (org, namespace, source_id) DO NOTHING
"""
_OWNER = """
SELECT user_id FROM loom.mcp_worker_refresh_admission
WHERE org = $1 AND namespace = $2 AND source_id = $3 FOR UPDATE
"""
_QUEUE = f"""
SELECT item_key, state, (updated_at <= clock_timestamp() - interval '24 hours') AS expired
FROM loom.knowledge_source_refresh WHERE {_SCOPE} ORDER BY item_key LIMIT $4
"""
_TRIM = f"""
DELETE FROM loom.knowledge_source_refresh WHERE {_SCOPE}
AND state IN ('completed', 'cancelled', 'exhausted') AND item_key = ANY($4::text[])
"""
_PENDING = frozenset({"pending", "claimed"})


@dataclass(frozen=True, slots=True, repr=False)
class AdmissionSource:
    org: str
    namespace: str
    source_id: UUID
    agent: str
    user_id: str
    session_id: str
    actor: str


async def lock_namespace_on(conn: Connection, org: str, namespace: str) -> None:
    if not pg.enabled() or not conn.is_in_transaction():
        raise RefreshRejected("source refresh requires Postgres authority transaction")
    await conn.execute(
        f"SELECT pg_advisory_xact_lock_shared({pg.KNOWLEDGE_LOCK_KEY})", org, namespace
    )


async def lock_source_on(conn: Connection, source: AdmissionSource) -> tuple[str, str, UUID]:
    await lock_namespace_on(conn, source.org, source.namespace)
    key = (source.org, source.namespace, source.source_id)
    await conn.execute(_OPEN, *key, source.agent, source.user_id, source.session_id, source.actor)
    owner = await conn.fetchval(_OWNER, *key)
    if owner != source.user_id:
        raise RefreshRejected("source refresh authority changed")
    return key


async def reserve_on(conn: Connection, source: AdmissionSource, count: int) -> bool:
    """Reserve one batch under the source's erasure and admission locks.

    The caller inserts the entire batch in this transaction. A busy source or
    retained receipts at capacity leave the due cursor untouched.
    """
    if type(count) is not int or not 1 <= count <= MAX_DOCUMENTS_PER_JOB:
        raise RefreshRejected("invalid refresh admission size")
    key = await lock_source_on(conn, source)
    rows = await conn.fetch(_QUEUE, *key, MAX_RETAINED + 1)
    if len(rows) > MAX_RETAINED:
        raise RefreshRejected("source refresh admission invariant failed")
    if any(row["state"] in _PENDING for row in rows):
        return False
    expired = [row["item_key"] for row in rows if row["expired"]]
    if len(rows) - len(expired) + count > MAX_RETAINED:
        return False
    if expired:
        await conn.execute(_TRIM, *key, expired)
    return True
