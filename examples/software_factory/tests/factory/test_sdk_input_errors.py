"""Selected input errors are recoverable; authorization and scope errors are fatal."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from loom.errors import AuthInsufficient, BackendUnavailable, ValidationError

from examples.software_factory.factory.adapters import SDKContext


class SDKInputErrors(unittest.TestCase):
    def adapter(self, error, *, scope_changed=False, verify_error=False):
        scope = {
            "org": "fixture",
            "namespace": "frozen",
            "repository": "authored/offline-fixtures",
            "commit": "a" * 40,
        }
        sent = [0]
        checks = [0]

        async def selected(*args, **kwargs):
            sent[0] += 1
            raise error

        async def verify(*args):
            sent[0] += 1
            checks[0] += 1
            if verify_error:
                raise ValidationError("Invalid scope verification request")
            observed = {**scope}
            if scope_changed and checks[0] > 1:
                observed["commit"] = "b" * 40
            return {"scope": observed, "permission": "allowed"}

        agent = SimpleNamespace(
            auto_run=False, mentor_brief=False, call_with_meta=AsyncMock(side_effect=selected)
        )
        adapter = SDKContext(agent, verify, lambda: sent[0], expected_scope=scope)
        self.addCleanup(adapter.close)
        return adapter, agent, checks

    def test_selected_validation_is_recoverable_and_all_requests_are_counted(self):
        native = ValidationError("invalid cursor: issued for a different query")
        adapter, agent, checks = self.adapter(native)
        with self.assertRaises(ValueError) as caught:
            adapter.request("code.grep", {"org": "fixture", "namespace": "frozen"})
        self.assertIn("invalid cursor", str(caught.exception))
        self.assertIs(caught.exception.__cause__, native)
        self.assertEqual(caught.exception.physical_requests, 3)
        self.assertEqual(checks[0], 2)
        self.assertEqual(agent.call_with_meta.await_count, 1)

    def test_changed_scope_after_input_error_remains_fatal(self):
        adapter, _, checks = self.adapter(ValidationError("invalid cursor"), scope_changed=True)
        with self.assertRaises(RuntimeError) as caught:
            adapter.request("code.grep", {"org": "fixture", "namespace": "frozen"})
        self.assertIsInstance(caught.exception.__cause__, PermissionError)
        self.assertEqual(caught.exception.physical_requests, 3)
        self.assertEqual(checks[0], 2)

    def test_scope_verification_validation_is_not_model_input(self):
        adapter, agent, _ = self.adapter(ValidationError("invalid cursor"), verify_error=True)
        with self.assertRaises(RuntimeError) as caught:
            adapter.request("code.grep", {"org": "fixture", "namespace": "frozen"})
        self.assertEqual(caught.exception.physical_requests, 1)
        self.assertEqual(agent.call_with_meta.await_count, 0)

    def test_auth_dependency_and_unclassified_failures_remain_fatal(self):
        classified = ValidationError("Forbidden input")
        classified.detail = {"category": "auth"}
        for error in (
            AuthInsufficient("Forbidden"),
            BackendUnavailable("Offline"),
            classified,
            ValueError("Unclassified failure"),
        ):
            with self.subTest(error=error):
                adapter, agent, checks = self.adapter(error)
                with self.assertRaises(RuntimeError) as caught:
                    adapter.request("code.grep", {"org": "fixture", "namespace": "frozen"})
                self.assertIs(caught.exception.__cause__, error)
                self.assertEqual(caught.exception.physical_requests, 2)
                self.assertEqual(checks[0], 1)
                self.assertEqual(agent.call_with_meta.await_count, 1)
