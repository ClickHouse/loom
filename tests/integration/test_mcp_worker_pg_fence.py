"""Check the source worker's frame handoff against real Postgres revocations."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org
from tests.integration.test_pg_authority import pg_authority as pg_authority
from tests.integration.test_pg_authority import pg_conn as pg_conn


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["unlink", "account", "approval"])
async def test_live_delivery_serializes_with_revocation(pg_conn: Any, revocation: str) -> None:
    from server.core import pg
    from server.knowledge.app.mcp_worker_pg_directory import fence_live_delivery
    from server.knowledge.app.mcp_worker_read import WorkerReadRefused, WorkerReadScope
    from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
    from server.knowledge.store import pg_mcp_worker_approvals as approvals
    from server.knowledge.store import pg_mcp_worker_enrollment as accounts
    from server.knowledge.store import pg_mcp_worker_readers as readers

    org, namespace = unique_org("worker-pg-fence"), "team"
    worker_id, source_id = uuid4(), uuid4()
    profile, space = "a" * 64, "b" * 64
    approval_key = approvals.ApprovalKey(org, namespace, worker_id, profile)
    owner = accounts.AccountKey(org, namespace, "owner", worker_id, profile, uuid4())
    reader = accounts.AccountKey(org, namespace, "reader", worker_id, profile, uuid4())
    link_key = readers.LinkKey(org, namespace, source_id, reader.requester)
    origin = accounts.WriteOrigin("pg-fence", "-", "pg-fence", "asserted")
    approver = approvals.ApprovalOwner(owner.requester, origin)
    loop = asyncio.get_running_loop()
    frame_entered, release_frame = asyncio.Event(), asyncio.Event()
    revoker_ready = loop.create_future()
    order: list[str] = []

    async def deliver() -> None:
        frame_entered.set()
        await release_frame.wait()
        order.append("frame")

    async def revoke() -> None:
        async with approvals.scope_transaction(approval_key) as conn:
            revoker_ready.set_result(conn.get_server_pid())
            await frame_entered.wait()
            if revocation == "unlink":
                assert await readers.revoke_on(conn, link_key, expected_link_generation=link_gen)
            elif revocation == "account":
                assert await accounts.revoke_account(conn, reader, generation=1, origin=origin)
            else:
                retired = await approvals.replace_on(
                    conn,
                    approval_key,
                    expected_generation=approval.generation,
                    modes=0,
                    team_access=False,
                    owner=approver,
                )
                assert retired is not None and retired.generation > approval.generation
                assert retired == approvals.WorkerApproval(retired.generation, 0, False, False)
        order.append("revoked")

    try:
        async with asyncio.timeout(15):
            async with approvals.scope_transaction(approval_key) as conn:
                approval = await approvals.replace_on(
                    conn,
                    approval_key,
                    expected_generation=0,
                    modes=approvals.LIVE,
                    team_access=True,
                    owner=approver,
                )
                assert approval is not None
                assert approval == approvals.WorkerApproval(
                    approval.generation, approvals.LIVE, True, True
                )
                issued_at = await conn.fetchval(
                    "SELECT floor(extract(epoch FROM statement_timestamp()))::bigint"
                )
                for account in (owner, reader):
                    challenge = accounts.Challenge(account, 1, uuid4(), issued_at, issued_at + 60)
                    assert await accounts.issue_challenge(conn, challenge, origin)
                    assert await accounts.consume_challenge(conn, challenge, resource_space=space)
                    assert await accounts.get_account(conn, account) == accounts.AccountState(
                        1, True, space
                    )
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
                active_link = readers.Link(1, reader.connection_id, 1, link_gen, True)
                assert await readers.current_on(conn, link_key) == active_link

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
                source_owner=ReaderConnection(
                    owner.requester, worker_id, owner.connection_id, 1, profile
                ),
            )

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(revoke())
                revoker_pid = await revoker_ready

                async def revoker_blocked() -> bool:
                    return await pg_conn.fetchval(
                        "SELECT cardinality(pg_blocking_pids($1)) > 0", revoker_pid
                    )

                tasks.create_task(fence_live_delivery(scope, deliver, loop.time() + 5))
                await frame_entered.wait()
                await eventually(
                    revoker_blocked,
                    max_wait=1,
                    interval=0.01,
                    description="Postgres blocks revocation behind the held frame",
                )
                release_frame.set()
            assert order == ["frame", "revoked"]

            if revocation == "unlink":
                unlinked = await readers.current_on(pg_conn, link_key)
                assert unlinked is not None and unlinked.link_generation > link_gen
                assert unlinked == replace(
                    active_link, active=False, link_generation=unlinked.link_generation
                )
            elif revocation == "account":
                assert await accounts.get_account(pg_conn, reader) == accounts.AccountState(
                    1, False, space
                )

            with pytest.raises(WorkerReadRefused, match="^denied$"):
                await fence_live_delivery(scope, deliver, loop.time() + 5)
            assert order == ["frame", "revoked"]
    finally:
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
