import json
import unittest
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
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
    replay_completion,
)


class TestRefreshReceipt(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        scope = RefreshScope(
            "org", "team", "alice", "source", "connection", "copy", 2, 4, 5, "b" * 64
        )
        task = DocumentTask("job", scope, "urn:source:selected")
        self.lease = LiveLease(task, str(uuid4()), 3, datetime.now(UTC) - timedelta(seconds=2))
        self.receipt = CompletionReceipt(
            task.idempotency_key, Observation(ObservationKind.PRESENT, "a" * 64)
        )
        self.conn = AsyncMock()
        self.conn.fetchrow.return_value = {
            "task": json.dumps({"version": 1, "task": asdict(task)}),
            "receipt": json.dumps(asdict(self.receipt)),
            "user_id": "alice",
            "claimable_at": self.lease.expires_at,
        }
        self.authorize = AsyncMock(
            return_value=CompletionAuthority(task, self.lease.worker_id, "copy", "alice")
        )
        self.completion = WorkerCompletion(self.lease, self.receipt, self.authorize)
        enabled = patch("server.knowledge.store.pg_worker_refresh.pg.enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)

    async def test_exact_receipt_replays_after_expiry_without_publication(self) -> None:
        self.assertTrue(await replay_completion(self.conn, self.completion))
        self.authorize.assert_awaited_once_with(self.conn, self.lease)
        self.conn.execute.assert_not_awaited()
        self.conn.fetchval.assert_not_awaited()

    async def test_revoked_or_different_user_does_not_receive_terminal_receipt(self) -> None:
        for authority in (None, replace(self.authorize.return_value, user_id="bob")):
            self.authorize.return_value = authority
            self.assertFalse(await replay_completion(self.conn, self.completion))

    async def test_same_fence_does_not_replay_a_changed_completion(self) -> None:
        changed = replace(self.receipt, observation=Observation(ObservationKind.PRESENT, "b" * 64))
        with self.assertRaisesRegex(RefreshRejected, "receipt conflicts"):
            await replay_completion(self.conn, replace(self.completion, receipt=changed))
