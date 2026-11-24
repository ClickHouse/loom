"""Check the source worker's frame handoff against real Postgres revocations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org
from tests.integration.test_pg_authority import pg_authority as pg_authority
from tests.integration.test_pg_authority import pg_conn as pg_conn

if TYPE_CHECKING:
    from asyncpg import Connection

    from server.knowledge.app.mcp_worker_read import WorkerReadScope
    from server.knowledge.store import pg_mcp_worker_approvals as approvals
    from server.knowledge.store import pg_mcp_worker_enrollment as accounts
    from server.knowledge.store import pg_mcp_worker_readers as readers


@dataclass(frozen=True)
class _Case:
    scope: WorkerReadScope
    approval_key: approvals.ApprovalKey
    approver: approvals.ApprovalOwner
    reader: accounts.AccountKey
    link_key: readers.LinkKey
    link: readers.Link
    realm: str


async def _seed_accounts(
    conn: Connection,
    owner: accounts.AccountKey,
    reader: accounts.AccountKey,
    origin: accounts.WriteOrigin,
    realm: str,
) -> None:
    from server.knowledge.store import pg_mcp_worker_enrollment as accounts

    issued_at = await conn.fetchval(
        "SELECT floor(extract(epoch FROM statement_timestamp()))::bigint"
    )
    for account in (owner, reader):
        challenge = accounts.Challenge(account, 1, uuid4(), issued_at, issued_at + 60)
        assert await accounts.issue_challenge(conn, challenge, origin)
        assert await accounts.consume_challenge(conn, challenge, resource_space=realm)
        assert await accounts.get_account(conn, account) == accounts.AccountState(1, True, realm)


async def _seed_source(org: str) -> _Case:
    from server.knowledge.app.mcp_worker_read import WorkerReadScope
    from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
    from server.knowledge.store import pg_mcp_worker_approvals as approvals
    from server.knowledge.store import pg_mcp_worker_enrollment as accounts
    from server.knowledge.store import pg_mcp_worker_readers as readers

    namespace, worker_id, source_id = "team", uuid4(), uuid4()
    profile, realm = "a" * 64, "b" * 64
    key = approvals.ApprovalKey(org, namespace, worker_id, profile)
    owner = accounts.AccountKey(org, namespace, "owner", worker_id, profile, uuid4())
    reader = accounts.AccountKey(org, namespace, "reader", worker_id, profile, uuid4())
    link_key = readers.LinkKey(org, namespace, source_id, reader.requester)
    origin = accounts.WriteOrigin("pg-fence", "-", "pg-fence", "asserted")
    approver = approvals.ApprovalOwner(owner.requester, origin)
    async with approvals.scope_transaction(key) as conn:
        approval = await approvals.replace_on(
            conn, key, expected_generation=0, modes=approvals.LIVE, team_access=True, owner=approver
        )
        assert approval is not None
        assert approval == approvals.WorkerApproval(approval.generation, approvals.LIVE, True, True)
        await _seed_accounts(conn, owner, reader, origin, realm)
        link_gen = await readers.set_on(
            conn,
            link_key,
            selection_generation=1,
            connection_id=reader.connection_id,
            connection_generation=1,
            expected_link_generation=0,
            origin=origin,
        )
        assert link_gen is not None
        link = readers.Link(1, reader.connection_id, 1, link_gen, True)
        assert await readers.current_on(conn, link_key) == link
    scope = WorkerReadScope(
        worker_id=worker_id,
        org=org,
        namespace=namespace,
        source_id=source_id,
        selection_generation=1,
        requester=reader.requester,
        connection_id=reader.connection_id,
        connection_generation=1,
        profile_digest=profile,
        approval_generation=approval.generation,
        resource_id="synthetic-resource",
        enrollment_generation=link_gen,
        source_owner=ReaderConnection(owner.requester, worker_id, owner.connection_id, 1, profile),
    )
    return _Case(scope, key, approver, reader, link_key, link, realm)


async def _apply_revocation(conn: Connection, case: _Case, revocation: str) -> None:
    from server.knowledge.store import pg_mcp_worker_approvals as approvals
    from server.knowledge.store import pg_mcp_worker_enrollment as accounts
    from server.knowledge.store import pg_mcp_worker_readers as readers

    if revocation == "unlink":
        assert await readers.revoke_on(
            conn, case.link_key, expected_link_generation=case.link.link_generation
        )
    elif revocation == "account":
        assert await accounts.revoke_account(
            conn, case.reader, generation=1, origin=case.approver.origin
        )
    else:
        generation = case.scope.approval_generation
        retired = await approvals.replace_on(
            conn,
            case.approval_key,
            expected_generation=generation,
            modes=0,
            team_access=False,
            owner=case.approver,
        )
        assert retired is not None and retired.generation > generation
        assert retired == approvals.WorkerApproval(retired.generation, 0, False, False)


async def _assert_revoked(pg_conn: Connection, case: _Case, revocation: str) -> None:
    from server.knowledge.store import pg_mcp_worker_enrollment as accounts
    from server.knowledge.store import pg_mcp_worker_readers as readers

    if revocation == "unlink":
        unlinked = await readers.current_on(pg_conn, case.link_key)
        assert unlinked is not None and unlinked.link_generation > case.link.link_generation
        assert unlinked == replace(
            case.link, active=False, link_generation=unlinked.link_generation
        )
    elif revocation == "account":
        assert await accounts.get_account(pg_conn, case.reader) == accounts.AccountState(
            1, False, case.realm
        )


async def _assert_publication_order(pg_conn: Connection, case: _Case, revocation: str) -> None:
    from server.knowledge.app.mcp_worker_pg_directory import fence_live_delivery
    from server.knowledge.app.mcp_worker_read import WorkerReadRefused
    from server.knowledge.store import pg_mcp_worker_approvals as approvals

    loop = asyncio.get_running_loop()
    frame_entered, release_frame = asyncio.Event(), asyncio.Event()
    revoker_ready: asyncio.Future[int] = loop.create_future()
    order: list[str] = []

    async def deliver() -> None:
        frame_entered.set()
        await release_frame.wait()
        order.append("frame")

    async def revoke() -> None:
        async with approvals.scope_transaction(case.approval_key) as conn:
            revoker_ready.set_result(conn.get_server_pid())
            await frame_entered.wait()
            await _apply_revocation(conn, case, revocation)
        order.append("revoked")

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(revoke())
        revoker_pid = await revoker_ready

        async def revoker_blocked() -> bool:
            return await pg_conn.fetchval(
                "SELECT cardinality(pg_blocking_pids($1)) > 0", revoker_pid
            )

        tasks.create_task(fence_live_delivery(case.scope, deliver, loop.time() + 5))
        await frame_entered.wait()
        await eventually(
            revoker_blocked,
            max_wait=1,
            interval=0.01,
            description="Postgres blocks revocation behind the held frame",
        )
        release_frame.set()
    assert order == ["frame", "revoked"]
    await _assert_revoked(pg_conn, case, revocation)
    with pytest.raises(WorkerReadRefused, match="^denied$"):
        await fence_live_delivery(case.scope, deliver, loop.time() + 5)
    assert order == ["frame", "revoked"]


async def _cleanup(pg_conn: Connection, org: str) -> None:
    from server.core import pg

    try:
        async with asyncio.timeout(10), pg_conn.transaction():
            for statement in (
                "DELETE FROM loom.mcp_worker_source_readers WHERE org = $1",
                "DELETE FROM loom.mcp_worker_enrollment_challenges WHERE org = $1",
                "DELETE FROM loom.mcp_worker_accounts WHERE org = $1",
                "DELETE FROM loom.mcp_worker_approvals WHERE org = $1",
            ):
                await pg_conn.execute(statement, org)
    finally:
        async with asyncio.timeout(10):
            await pg.close_pool()


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["unlink", "account", "approval"])
async def test_live_delivery_serializes_with_revocation(
    pg_conn: Connection, revocation: str
) -> None:
    org = unique_org("worker-pg-fence")
    try:
        async with asyncio.timeout(15):
            await _assert_publication_order(pg_conn, await _seed_source(org), revocation)
    finally:
        await _cleanup(pg_conn, org)
