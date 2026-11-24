import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from server.knowledge.app.mcp_worker_pg_directory import current_connection, current_enrollment
from server.knowledge.store import pg_mcp_worker_enrollment as accounts
from server.knowledge.store import pg_mcp_worker_readers as readers


class TestWorkerReaderDirectory(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(readers.pg, "enabled", return_value=True))
        self.worker, self.connection, self.source = UUID(int=2), UUID(int=3), UUID(int=4)
        self.key = readers.LinkKey("org", "team", self.source, "bob")
        self.origin = accounts.WriteOrigin("agent", "-", "bob", "asserted")

    async def test_directory_does_not_return_a_disconnected_link(self) -> None:
        with patch.object(readers, "current", AsyncMock()) as get:
            get.return_value = readers.Link(4, self.connection, 3, 2, False)
            self.assertIsNone(await current_enrollment(*self.key.parameters()))
            get.assert_awaited_once_with(self.key)

    async def test_directory_binds_connection_to_exact_org_and_owner(self) -> None:
        args = ("org", "bob", self.worker, self.connection)
        result = ("a" * 64, accounts.AccountState(2, True, "b" * 64))
        with patch.object(
            accounts, "current_reader_account", AsyncMock(return_value=result)
        ) as get:
            current = await current_connection(*args)
            get.assert_awaited_once_with(*args)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.binding.owner, "bob")
            self.assertEqual(current.binding.connection_id, self.connection)

    async def test_stale_change_does_not_overwrite_a_link(self) -> None:
        conn = Mock(
            is_in_transaction=Mock(return_value=True), fetchval=AsyncMock(return_value=None)
        )
        result = await readers.set_on(
            conn,
            self.key,
            selection_generation=5,
            connection_id=self.connection,
            connection_generation=2,
            expected_link_generation=7,
            origin=self.origin,
        )
        query, *bound = conn.fetchval.await_args.args
        self.assertIsNone(result)
        self.assertIn("link_generation = $12 AND selection_generation <= $5", query)
        self.assertEqual(tuple(bound[:4]), self.key.parameters())
        self.assertEqual(bound[-1], 7)
