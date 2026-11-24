import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from server.knowledge.store import pg_mcp_worker_approvals as approvals
from server.knowledge.store.pg_mcp_worker_enrollment import WriteOrigin


class TestWorkerApprovals(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(approvals.pg, "enabled", return_value=True))
        self.key = approvals.ApprovalKey("org", "team", UUID(int=2), "a" * 64)
        self.owner = approvals.ApprovalOwner(
            "alice", WriteOrigin("agent", "-", "alice", "asserted")
        )
        self.conn = Mock(is_in_transaction=Mock(return_value=True), fetchrow=AsyncMock())

    async def test_approval_is_locked_and_scoped_to_one_exact_profile(self) -> None:
        self.conn.fetchrow.return_value = {
            "generation": 3,
            "modes": approvals.LIVE,
            "team_access": False,
            "active": True,
        }
        entry = await approvals.current_on(self.conn, self.key, lock=True)
        self.assertEqual(entry, approvals.WorkerApproval(3, approvals.LIVE, False, True))
        query, *args = self.conn.fetchrow.await_args.args
        self.assertIn(
            "org = $1 AND namespace = $2 AND worker_id = $3 AND profile_digest = $4", query
        )
        self.assertTrue(query.endswith(" FOR SHARE"))
        self.assertEqual(tuple(args), self.key.arguments())

    async def test_stale_approval_cannot_write_or_reuse_generation(self) -> None:
        self.conn.fetchrow.return_value = None
        outcome = await approvals.replace_on(
            self.conn,
            self.key,
            expected_generation=4,
            modes=0,
            team_access=False,
            owner=self.owner,
        )
        self.assertIsNone(outcome)
        query, *args = self.conn.fetchrow.await_args.args
        self.assertIn("AND generation = $12", query)
        self.assertIn("nextval('loom.mcp_worker_approval_generations')", query)
        self.assertEqual(args[-1], 4)

    async def test_a_new_approval_cannot_overwrite_an_existing_scope(self) -> None:
        self.conn.fetchrow.return_value = None
        self.assertIsNone(
            await approvals.replace_on(
                self.conn,
                self.key,
                expected_generation=0,
                modes=approvals.LIVE,
                team_access=True,
                owner=self.owner,
            )
        )
        query, *_ = self.conn.fetchrow.await_args.args
        self.assertIn("ON CONFLICT (org, namespace, worker_id, profile_digest) DO NOTHING", query)

    async def test_invalid_or_untransactional_change_never_queries_the_store(self) -> None:
        self.assertIsNone(
            await approvals.replace_on(
                self.conn,
                self.key,
                expected_generation=1,
                modes=0,
                team_access=True,
                owner=self.owner,
            )
        )
        self.conn.is_in_transaction.return_value = False
        with self.assertRaises(RuntimeError):
            await approvals.current_on(self.conn, self.key, lock=True)
        self.conn.fetchrow.assert_not_awaited()
