import asyncio
import unittest
from dataclasses import replace
from unittest.mock import create_autospec, patch
from uuid import UUID

from server.knowledge.app.mcp_worker_read import WorkerReadChannel, WorkerReadRefused
from server.knowledge.app.mcp_worker_selection import SelectionAuthority, verify_worker_selection
from server.knowledge.domain.mcp_identity import ResourceIdentity, selection_sha256
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection
from server.settings import settings


class TestSelectionVerification(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        connection = ReaderConnection("alice", UUID(int=1), UUID(int=2), 3, "b" * 64)
        self.authority = SelectionAuthority(
            "org", "team", UUID(int=4), UUID(int=5), 6, 7, connection
        )
        self.standing: SelectionAuthority | None = self.authority
        self.resources = tuple(
            ResourceIdentity(id=f"urn:document:{number}", type="resource") for number in range(5)
        )
        self.channel = create_autospec(WorkerReadChannel, instance=True)
        self.started: list[str] = []
        self.inflight = 0
        self.peak = 0

    async def _current(self):
        return self.standing

    async def _check_only(self, scope, operation, require_current):
        self.assertEqual(operation, "check_access")
        self.assertEqual(scope, await require_current())
        self.assertEqual(scope.connection_id, self.authority.connection.connection_id)
        self.started.append(scope.resource_id)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0)
            self.assertEqual(scope, await require_current())
        finally:
            self.inflight -= 1

    async def _verify(self):
        return await verify_worker_selection(
            self.authority,
            self.resources,
            mode="snapshot",
            channel=self.channel,
            current=self._current,
            deadline=asyncio.get_running_loop().time() + 1,
        )

    async def test_all_resources_are_checked_under_sealed_destination_and_bounded_concurrency(self):
        self.channel.run.side_effect = self._check_only
        receipt = await self._verify()
        self.assertEqual(self.started, [item.id for item in self.resources])
        self.assertEqual(self.peak, 2)
        self.assertEqual(self.inflight, 0)
        self.assertEqual(receipt.resources_sha256, selection_sha256(self.resources))
        self.assertEqual(receipt.destination_id, self.authority.destination_id)
        self.assertEqual(receipt.source_id, self.authority.source_id)
        self.assertEqual(receipt.selection_generation, self.authority.selection_generation)

    async def test_configured_serial_checks_still_verify_the_whole_selection(self):
        self.channel.run.side_effect = self._check_only
        with patch.object(settings, "knowledge_source_worker_selection_parallelism", 1):
            receipt = await self._verify()
        self.assertEqual(self.started, [item.id for item in self.resources])
        self.assertEqual(self.peak, 1)
        self.assertEqual(receipt.resources_sha256, selection_sha256(self.resources))

    async def test_revocation_cancels_batch_without_a_grant_or_remaining_requests(self):
        async def read(scope, operation, require_current):
            if scope.resource_id == self.resources[0].id:
                self.standing = replace(self.authority, approval_generation=8)
            await self._check_only(scope, operation, require_current)

        self.channel.run.side_effect = read
        with self.assertRaisesRegex(WorkerReadRefused, "^denied$"):
            await self._verify()
        self.assertEqual(self.channel.run.await_count, 2)
        self.assertEqual(self.inflight, 0)
