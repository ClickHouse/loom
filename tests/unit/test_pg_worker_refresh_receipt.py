import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from server.knowledge.domain.mcp_worker_refresh import (
    CompletionReceipt,
    DocumentTask,
    LiveLease,
    Observation,
    ObservationKind,
    RefreshRejected,
    RefreshScope,
)
from server.knowledge.store.pg_worker_refresh import (
    CompletionAuthority,
    WorkerCompletion,
    checkpoint,
    lock_current,
    replay_completion,
)


class TestRefreshReceipt(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        scope = RefreshScope(
            "org", "team", "alice", "source", "connection", "copy", 2, 11, 4, 5, "b" * 64
        )
        task = DocumentTask("job", scope, "urn:source:selected")
        self.lease = LiveLease(task, str(uuid4()), 3, datetime.now(UTC) - timedelta(seconds=2))
        self.receipt = CompletionReceipt(
            task.idempotency_key, Observation(ObservationKind.PRESENT, "a" * 64)
        )
        self.conn = AsyncMock(is_in_transaction=Mock(return_value=True))
        self.conn.fetchrow.return_value = {
            "task": {"version": 1, "task": asdict(task)},
            "receipt": asdict(self.receipt),
            "user_id": "alice",
            "claimable_at": self.lease.expires_at,
        }
        self.authorize = AsyncMock(
            return_value=CompletionAuthority(task, self.lease.worker_id, "alice")
        )
        self.completion = WorkerCompletion(self.lease, self.receipt, self.authorize)
        enabled = patch("server.knowledge.store.pg_worker_refresh.pg.enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    async def test_exact_receipt_replays_after_expiry_without_publication(self) -> None:
        self.assertTrue(await replay_completion(self.conn, self.completion))
        self.authorize.assert_awaited_once_with(self.conn, self.lease)
        self.conn.execute.assert_awaited_once()
        assert "pg_advisory_xact_lock_shared" in self.conn.execute.await_args.args[0]
        self.conn.fetchval.assert_not_awaited()

    async def test_terminal_row_is_locked_only_after_authority_and_must_still_exist(self) -> None:
        initial = self.conn.fetchrow.return_value
        self.conn.fetchrow.side_effect = [initial, None]

        async def authorize(conn, current):
            assert conn is self.conn and current == self.lease
            assert self.conn.fetchrow.await_count == 1
            assert "FOR SHARE" not in self.conn.fetchrow.await_args.args[0]
            return CompletionAuthority(self.lease.task, self.lease.worker_id, "alice")

        completion = replace(self.completion, authorize=authorize)
        self.assertFalse(await replay_completion(self.conn, completion))
        assert self.conn.fetchrow.await_count == 2
        assert "FOR SHARE" in self.conn.fetchrow.await_args.args[0]

    async def test_revoked_or_different_user_does_not_receive_terminal_receipt(self) -> None:
        for authority in (None, replace(self.authorize.return_value, user_id="bob")):
            self.authorize.return_value = authority
            self.assertFalse(await replay_completion(self.conn, self.completion))

    async def test_terminal_receipt_refuses_a_new_source_generation(self) -> None:
        old = self.lease.task
        newer = replace(
            old, scope=replace(old.scope, source_generation=old.scope.source_generation + 1)
        )
        self.authorize.return_value = replace(self.authorize.return_value, task=newer)
        self.assertFalse(await replay_completion(self.conn, self.completion))
        self.conn.execute.assert_awaited_once()
        assert "pg_advisory_xact_lock_shared" in self.conn.execute.await_args.args[0]
        self.conn.fetchval.assert_not_awaited()

    async def test_same_fence_does_not_replay_a_changed_completion(self) -> None:
        changed = replace(self.receipt, observation=Observation(ObservationKind.PRESENT, "b" * 64))
        with self.assertRaisesRegex(RefreshRejected, "receipt conflicts"):
            await replay_completion(self.conn, replace(self.completion, receipt=changed))

    async def test_other_destination_cannot_replay_terminal_receipt(self) -> None:
        task = self.lease.task
        different = replace(task, scope=replace(task.scope, destination_id="other-copy"))
        self.authorize.return_value = replace(self.authorize.return_value, task=different)
        self.assertFalse(await replay_completion(self.conn, self.completion))

    async def test_autocommit_cannot_lock_or_settle_work(self) -> None:
        self.conn.is_in_transaction.return_value = False
        expected = "requires Postgres authority transaction"
        with self.assertRaisesRegex(RefreshRejected, expected):
            await lock_current(self.conn, self.lease)
        with self.assertRaisesRegex(RefreshRejected, expected):
            await checkpoint(self.conn, self.lease, self.receipt)
        with self.assertRaisesRegex(RefreshRejected, expected):
            await replay_completion(self.conn, self.completion)
        self.conn.fetchrow.assert_not_awaited()
        self.conn.fetchval.assert_not_awaited()
        self.authorize.assert_not_awaited()
