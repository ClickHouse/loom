"""Verify the store's transaction and SQL contracts without a Postgres process."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call
from uuid import UUID

import pytest

from server.knowledge.store import pg_mcp_worker_enrollment as enrollment

_ORG = "example-org"
_NAMESPACE = "account-owner-scope"
_REQUESTER = "requester"
_WORKER = UUID(int=1)
_PROFILE = "a" * 64
_CONNECTION = UUID(int=2)
_NONCE = UUID(int=3)
_ORIGIN = enrollment.WriteOrigin("agent", "-", "actor", "direct")
_GENERATION = 3
_ISSUED = 1_000
_EXPIRY = _ISSUED + 120
_RESOURCE_SPACE = "b" * 64
_ACCOUNT_ARGS = (_ORG, _NAMESPACE, _REQUESTER, _WORKER, _PROFILE, _CONNECTION)
_ACCOUNT = enrollment.AccountKey(*_ACCOUNT_ARGS)
_CHALLENGE = enrollment.Challenge(_ACCOUNT, _GENERATION, _NONCE, _ISSUED, _EXPIRY)
_CHALLENGE_ARGS = (*_ACCOUNT_ARGS, _GENERATION, _NONCE, _ISSUED, _EXPIRY)
_ORIGIN_ARGS = ("agent", "-", "actor", "direct")


@pytest.fixture
def conn() -> Mock:
    return Mock(
        is_in_transaction=Mock(return_value=True),
        execute=AsyncMock(),
        fetchval=AsyncMock(return_value=True),
        fetchrow=AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_issue_binds_fresh_nonce_and_origin(conn: Mock) -> None:
    assert await enrollment.issue_challenge(conn, _CHALLENGE, _ORIGIN)
    conn.execute.assert_awaited_once_with(enrollment._LOCK, _ORG, _CONNECTION)
    conn.fetchval.assert_awaited_once_with(enrollment._ISSUE, *_CHALLENGE_ARGS, *_ORIGIN_ARGS)
    sql = enrollment._ISSUE
    assert "$9::bigint <= extract(epoch FROM statement_timestamp())" in sql
    assert "$10::bigint > extract(epoch FROM statement_timestamp())" in sql
    assert "org = $1 AND connection_id = $6 AND NOT consumed" in sql
    assert "ON CONFLICT (org, nonce) DO NOTHING" in sql
    conn.fetchval.return_value = None
    assert not await enrollment.issue_challenge(conn, _CHALLENGE, _ORIGIN)


@pytest.mark.asyncio
@pytest.mark.parametrize("lifetime", [-1, 0, 121])
async def test_issue_rejects_out_of_range_lifetime(conn: Mock, lifetime: int) -> None:
    challenge = replace(_CHALLENGE, expires_at=_ISSUED + lifetime)
    assert not await enrollment.issue_challenge(conn, challenge, _ORIGIN)
    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_writes_require_callers_transaction(conn: Mock) -> None:
    conn.is_in_transaction.return_value = False
    with pytest.raises(RuntimeError, match="requires an authority transaction"):
        await enrollment.issue_challenge(conn, _CHALLENGE, _ORIGIN)
    with pytest.raises(RuntimeError, match="requires an authority transaction"):
        await enrollment.consume_challenge(conn, _CHALLENGE, resource_space=None)
    with pytest.raises(RuntimeError, match="requires an authority transaction"):
        await enrollment.revoke_account(conn, _ACCOUNT, generation=_GENERATION, origin=_ORIGIN)
    conn.fetchval.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_consume_serializes_and_binds_the_entire_expected_challenge(conn: Mock) -> None:
    assert await enrollment.consume_challenge(conn, _CHALLENGE, resource_space=_RESOURCE_SPACE)
    assert conn.mock_calls == [
        call.is_in_transaction(),
        call.execute(enrollment._LOCK, _ORG, _CONNECTION),
        call.fetchval(enrollment._CONSUME, *_CHALLENGE_ARGS, _RESOURCE_SPACE),
    ]
    sql = enrollment._CONSUME
    assert "pg_advisory_xact_lock" in enrollment._LOCK
    assert "SET consumed = true" in sql
    assert "generation = $7 AND nonce = $8" in sql
    assert "issued_at = $9 AND expires_at = $10 AND NOT consumed" in sql
    assert "expires_at > extract(epoch FROM statement_timestamp())" in sql
    assert "FROM claimed\n    ON CONFLICT (org, connection_id)" in sql
    assert "org = $1 AND namespace = $2 AND user_id = $3 AND worker_id = $4 " in sql
    assert "AND profile_digest = $5 AND connection_id = $6" in sql


def test_account_write_requires_same_binding_and_a_strictly_newer_generation() -> None:
    sql = enrollment._CONSUME
    assert "account.namespace = EXCLUDED.namespace AND account.user_id = EXCLUDED.user_id" in sql
    assert "AND account.worker_id = EXCLUDED.worker_id" in sql
    assert "AND account.profile_digest = EXCLUDED.profile_digest" in sql
    assert "account.resource_space IS NOT DISTINCT FROM EXCLUDED.resource_space" in sql
    assert "account.generation < EXCLUDED.generation" in sql
    assert "generation = $7 AND active" in sql
    assert "resource_space IS NOT DISTINCT FROM $11 AND EXISTS (SELECT FROM claimed)" in sql


@pytest.mark.asyncio
async def test_revocation_uses_same_lock_and_preserves_the_generation(conn: Mock) -> None:
    conn.fetchval.return_value = None
    assert not await enrollment.revoke_account(
        conn, _ACCOUNT, generation=_GENERATION, origin=_ORIGIN
    )
    assert conn.mock_calls == [
        call.is_in_transaction(),
        call.execute(enrollment._LOCK, _ORG, _CONNECTION),
        call.fetchval(enrollment._REVOKE, *_ACCOUNT_ARGS, _GENERATION, *_ORIGIN_ARGS),
    ]
    sql = enrollment._REVOKE
    assert "SET active = false" in sql
    assert "generation = $7 AND active" in sql
    assert "DELETE" not in sql


@pytest.mark.asyncio
async def test_get_requires_exact_owner_binding_and_returns_inactive_fence(conn: Mock) -> None:
    assert await enrollment.get_account(conn, _ACCOUNT) is None
    conn.fetchrow.return_value = {
        "generation": _GENERATION,
        "active": False,
        "resource_space": _RESOURCE_SPACE,
    }
    assert await enrollment.get_account(conn, _ACCOUNT) == enrollment.AccountState(
        _GENERATION, False, _RESOURCE_SPACE
    )
    assert conn.fetchrow.await_args_list == [call(enrollment._GET, *_ACCOUNT_ARGS)] * 2


@pytest.mark.asyncio
async def test_prune_limits_work_to_expired_challenges_in_one_org(conn: Mock) -> None:
    conn.fetchval.return_value = 2
    assert await enrollment.prune_expired_challenges(conn, _ORG) == 2
    conn.fetchval.assert_awaited_once_with(enrollment._PRUNE, _ORG)
    assert "WHERE org = $1 AND expires_at <=" in enrollment._PRUNE
    assert "LIMIT 1000 FOR UPDATE SKIP LOCKED" in enrollment._PRUNE
    assert "WHERE challenge.org = $1" in enrollment._PRUNE
    assert "loom.mcp_worker_accounts" not in enrollment._PRUNE


def test_migration_is_additive_and_stores_metadata_only() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "schemas/pg_migrations/015_mcp_worker_enrollment.sql"
    )
    ddl = migration.read_text(encoding="utf-8")
    assert ddl.count("CREATE TABLE IF NOT EXISTS") == 2
    assert "PRIMARY KEY (org, nonce)" in ddl
    assert "PRIMARY KEY (org, connection_id)" in ddl
    assert "CHECK (expires_at - issued_at BETWEEN 1 AND 120)" in ddl
    assert "resource_space ~ '^[0-9a-f]{64}$'" in ddl
    assert not any(
        field in ddl for field in ("credential", "token", "endpoint", "content", "jsonb", "bytea")
    )
