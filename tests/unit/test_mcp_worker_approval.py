import unittest
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID

from server.knowledge.app.mcp_worker_approval import (
    ApprovalChange,
    WorkerApprovalRefused,
    WorkerProvision,
    _can_approve,
    apply_worker_approval,
)
from server.knowledge.store.pg_mcp_worker_approvals import LIVE, REFRESH, SNAPSHOT, ApprovalKey


class TestWorkerApproval(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = ApprovalKey("org", "team", UUID(int=3), "b" * 64)
        self.change = ApprovalChange(self.scope, 4, LIVE, False)
        self.profile = WorkerProvision(self.scope, LIVE | SNAPSHOT, False)

    def test_cannot_approve_another_scope_or_unqualified_refresh_or_team_access(self) -> None:
        self.assertTrue(_can_approve(self.change, self.profile))
        self.assertFalse(_can_approve(replace(self.change, modes=LIVE | REFRESH), self.profile))
        self.assertFalse(_can_approve(replace(self.change, team_access=True), self.profile))
        for key in (
            replace(self.scope, org="other"),
            replace(self.scope, namespace="private"),
            replace(self.scope, profile_digest="a" * 64),
            replace(self.scope, worker_id=UUID(int=4)),
        ):
            self.assertFalse(_can_approve(self.change, replace(self.profile, key=key)))

    def test_removing_an_approval_does_not_require_live_provisioning(self) -> None:
        self.assertTrue(_can_approve(replace(self.change, modes=0), None))
        self.assertFalse(_can_approve(replace(self.change, modes=0, team_access=True), None))

    async def test_unprotected_request_cannot_look_up_worker_or_erasure_state(self) -> None:
        provision, erasure = AsyncMock(), AsyncMock()
        with self.assertRaises(WorkerApprovalRefused):
            await apply_worker_approval(self.change, provision=provision, not_erasing=erasure)
        provision.assert_not_awaited()
        erasure.assert_not_awaited()
