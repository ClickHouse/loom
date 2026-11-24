"""Opaque account enrollment under the caller's Postgres authority transaction."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.core import pg

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from asyncpg import Connection


@dataclass(frozen=True, repr=False)
class AccountKey:
    org: str
    namespace: str
    requester: str
    worker_id: UUID
    profile_digest: str
    connection_id: UUID


@dataclass(frozen=True, repr=False)
class Challenge:
    account: AccountKey
    generation: int
    nonce: UUID
    issued_at: int
    expires_at: int


@dataclass(frozen=True, repr=False)
class WriteOrigin:
    agent: str
    session_id: str
    actor: str
    source_class: str


@dataclass(frozen=True, repr=False)
class AccountState:
    generation: int
    active: bool
    resource_space: str | None


_ACCOUNT_SCOPE = (
    "org = $1 AND namespace = $2 AND user_id = $3 AND worker_id = $4 "
    "AND profile_digest = $5 AND connection_id = $6"
)

_ISSUE = """
INSERT INTO loom.mcp_worker_enrollment_challenges
    (org, namespace, user_id, worker_id, profile_digest, connection_id,
     generation, nonce, issued_at, expires_at, agent, session_id, actor, source_class)
SELECT $1::text, $2::text, $3::text, $4::uuid, $5::text, $6::uuid, $7::bigint, $8::uuid,
       $9::bigint, $10::bigint, $11::text, $12::text, $13::text, $14::text
WHERE $9::bigint <= extract(epoch FROM statement_timestamp())
  AND $10::bigint > extract(epoch FROM statement_timestamp())
  AND NOT EXISTS (
      SELECT 1 FROM loom.mcp_worker_enrollment_challenges
      WHERE org = $1 AND connection_id = $6 AND NOT consumed
        AND expires_at > extract(epoch FROM statement_timestamp())
  )
ON CONFLICT (org, nonce) DO NOTHING
RETURNING true
"""

_LOCK = """
SELECT pg_advisory_xact_lock(hashtextextended(
    jsonb_build_array('mcp_worker_account'::text, $1::text, $2::uuid)::text, 0))
"""

_CONSUME = f"""
WITH claimed AS (
    UPDATE loom.mcp_worker_enrollment_challenges
    SET consumed = true
    WHERE {_ACCOUNT_SCOPE} AND generation = $7 AND nonce = $8
      AND issued_at = $9 AND expires_at = $10 AND NOT consumed
      AND issued_at <= extract(epoch FROM statement_timestamp())
      AND expires_at > extract(epoch FROM statement_timestamp())
    RETURNING org, namespace, user_id, worker_id, profile_digest, connection_id,
              generation, agent, session_id, actor, source_class
), written AS (
    INSERT INTO loom.mcp_worker_accounts AS account
        (org, namespace, user_id, worker_id, profile_digest, connection_id,
         generation, agent, session_id, actor, source_class, resource_space, active)
    SELECT org, namespace, user_id, worker_id, profile_digest, connection_id,
           generation, agent, session_id, actor, source_class, $11::text, true
    FROM claimed
    ON CONFLICT (org, connection_id) DO UPDATE SET
        generation = EXCLUDED.generation, active = true, updated_at = clock_timestamp(),
        agent = EXCLUDED.agent, session_id = EXCLUDED.session_id,
        actor = EXCLUDED.actor, source_class = EXCLUDED.source_class
    WHERE account.namespace = EXCLUDED.namespace AND account.user_id = EXCLUDED.user_id
      AND account.worker_id = EXCLUDED.worker_id
      AND account.profile_digest = EXCLUDED.profile_digest
      AND account.resource_space IS NOT DISTINCT FROM EXCLUDED.resource_space
      AND account.generation < EXCLUDED.generation
    RETURNING true
)
SELECT EXISTS (SELECT FROM written) OR EXISTS (
    SELECT FROM loom.mcp_worker_accounts
    WHERE {_ACCOUNT_SCOPE} AND generation = $7 AND active
      AND resource_space IS NOT DISTINCT FROM $11 AND EXISTS (SELECT FROM claimed)
)
"""

_GET = f"""
SELECT generation, active, resource_space FROM loom.mcp_worker_accounts
WHERE {_ACCOUNT_SCOPE}
"""

_CURRENT_READER = """
SELECT profile_digest, generation, active, resource_space FROM loom.mcp_worker_accounts
WHERE org = $1 AND user_id = $2 AND worker_id = $3 AND connection_id = $4
"""

_REVOKE = f"""
UPDATE loom.mcp_worker_accounts
SET active = false, updated_at = clock_timestamp(),
    agent = $8, session_id = $9, actor = $10, source_class = $11
WHERE {_ACCOUNT_SCOPE} AND generation = $7 AND active
RETURNING true
"""

_PRUNE = """
WITH expired AS (
    SELECT nonce FROM loom.mcp_worker_enrollment_challenges
    WHERE org = $1 AND expires_at <= floor(extract(epoch FROM statement_timestamp()))::bigint
    ORDER BY expires_at, nonce LIMIT 1000 FOR UPDATE SKIP LOCKED
), removed AS (
    DELETE FROM loom.mcp_worker_enrollment_challenges AS challenge USING expired
    WHERE challenge.org = $1 AND challenge.nonce = expired.nonce RETURNING 1
)
SELECT count(*) FROM removed
"""


def _account_args(account: AccountKey) -> tuple[str, str, str, UUID, str, UUID]:
    return (
        account.org,
        account.namespace,
        account.requester,
        account.worker_id,
        account.profile_digest,
        account.connection_id,
    )


def _challenge_args(
    challenge: Challenge,
) -> tuple[str, str, str, UUID, str, UUID, int, UUID, int, int]:
    return (
        *_account_args(challenge.account),
        challenge.generation,
        challenge.nonce,
        challenge.issued_at,
        challenge.expires_at,
    )


def _origin_args(origin: WriteOrigin) -> tuple[str, str, str, str]:
    return origin.agent, origin.session_id, origin.actor, origin.source_class


def _require_transaction(conn: Connection) -> None:
    if not pg.enabled() or not conn.is_in_transaction():
        raise RuntimeError("account enrollment requires an authority transaction")


async def lock_account(conn: Connection, account: AccountKey) -> None:
    """Lock one account ID, including before its first write; use Read Committed."""
    _require_transaction(conn)
    await conn.execute(_LOCK, account.org, account.connection_id)


async def issue_challenge(conn: Connection, challenge: Challenge, origin: WriteOrigin) -> bool:
    """Insert a fresh server-generated nonce under current owner and approval fences."""
    _require_transaction(conn)
    if not 0 < challenge.expires_at - challenge.issued_at <= 120 or challenge.issued_at < 0:
        return False
    await conn.execute(_LOCK, challenge.account.org, challenge.account.connection_id)
    return (await conn.fetchval(_ISSUE, *_challenge_args(challenge), *_origin_args(origin))) is True


async def consume_challenge(
    conn: Connection, challenge: Challenge, *, resource_space: str | None
) -> bool:
    """Consume an authenticated binding and install its verified metadata atomically.

    The caller holds current owner, source and approval fences. A matched challenge
    is spent even on an account conflict if the caller commits the refusal.
    """
    await lock_account(conn, challenge.account)
    return (await conn.fetchval(_CONSUME, *_challenge_args(challenge), resource_space)) is True


async def get_account(conn: Connection, account: AccountKey) -> AccountState | None:
    """Read only the owner's exact account; inactive rows are generation fences."""
    if not pg.enabled():
        return None
    row = await conn.fetchrow(_GET, *_account_args(account))
    if row is None:
        return None
    return AccountState(row["generation"], row["active"], row["resource_space"])


async def current_reader_account(
    org: str, owner: str, worker_id: UUID, connection_id: UUID
) -> tuple[str, AccountState] | None:
    if not pg.enabled():
        return None
    async with pg.connection() as conn:
        return await reader_account_on(conn, org, owner, worker_id, connection_id)


async def reader_account_on(
    conn: Connection,
    org: str,
    owner: str,
    worker_id: UUID,
    connection_id: UUID,
    *,
    lock: bool = False,
) -> tuple[str, AccountState] | None:
    if not pg.enabled():
        return None
    if lock:
        _require_transaction(conn)
    query = _CURRENT_READER + " FOR SHARE NOWAIT" if lock else _CURRENT_READER
    row = await conn.fetchrow(query, org, owner, worker_id, connection_id)
    if row is None:
        return None
    return row["profile_digest"], AccountState(
        row["generation"], row["active"], row["resource_space"]
    )


async def revoke_account(
    conn: Connection, account: AccountKey, *, generation: int, origin: WriteOrigin
) -> bool:
    """Revoke the exact enrolled generation, retaining its fence against re-enrollment."""
    await lock_account(conn, account)
    return (
        await conn.fetchval(_REVOKE, *_account_args(account), generation, *_origin_args(origin))
    ) is True


async def prune_expired_challenges(conn: Connection, org: str) -> int:
    """Remove at most one org-scoped batch of expired challenges, never account fences."""
    _require_transaction(conn)
    return int(await conn.fetchval(_PRUNE, org))


@asynccontextmanager
async def owner_transaction(account: AccountKey) -> AsyncIterator[Connection]:
    """Take the existing knowledge scope lock before the account enrollment lock."""
    if not pg.enabled():
        raise RuntimeError("account enrollment requires Postgres authority")
    async with pg.transaction() as conn:
        await conn.execute(
            f"SELECT pg_advisory_xact_lock_shared({pg.KNOWLEDGE_LOCK_KEY})",
            account.org,
            account.namespace,
        )
        yield conn
