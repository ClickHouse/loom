from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from examples.software_factory.factory.adapters import ReplayContext, ReplayProvider
from examples.software_factory.factory.engine import Factory
from examples.software_factory.factory.model import Budget, Ledger
from examples.software_factory.factory.retrieval_tools import QualifiedContext
from examples.software_factory.factory.workspace import Checkout
from examples.software_factory.tests.factory.test_factory import ROOT
from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader
from examples.software_factory.tests.fixtures.python.grading import load_task as load_fixture


class LocalSourceScope(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = json.loads((ROOT / "suite.json").read_text())["tasks"]["name-validation"]
        self.task = load_fixture(ROOT / self.case["task"]["path"])
        self.backend = Mock(request=Mock(side_effect=AssertionError("Unexpected Loom dispatch")))
        self.ledger = Ledger(self.root / "events.jsonl")
        self.context = QualifiedContext(
            self.task,
            Checkout(self.root / "checkout", self.task),
            self.backend,
            self.ledger,
            Budget(),
            "fixture",
        )

    def test_ungroundable_prefix_refuses_locally_without_claiming_index_coverage(self):
        for op, args in (
            ("code.grep", {"pattern": "needle", "path_prefix": "tests"}),
            ("code.search", {"query": "needle", "path_prefix": "docs/"}),
        ):
            with self.subTest(op=op):
                result = self.context.selected(op, args)
                self.assertTrue(result["abstain"])
                self.assertEqual(result["abstain_reason"], "outside_factory_source_scope")
                self.assertEqual(result["scope"], "factory_source_allowlist")
                self.assertEqual(result["server_coverage"], "not_queried")
                self.assertFalse(result["request_dispatched"])
                self.assertIn("ordinary search/read", result["note"])
                self.assertNotIn("not_covered", result)
                self.assertNotIn("freshness", result)
        self.backend.request.assert_not_called()
        self.assertEqual(self.context.requests, 0)
        self.assertEqual(self.context.chars, 0)
        self.assertFalse(any(e["kind"] == "retrieval" for e in self.ledger.rows))
        refusals = [e for e in self.ledger.rows if e["kind"] == "local_scope_refusal"]
        self.assertEqual(len(refusals), 2)
        self.assertTrue(all(e["physical_requests"] == 0 for e in refusals))

    def test_local_refusal_needs_no_server_retrieval_budget(self):
        self.context.requests = self.context.budget.retrievals
        result = self.context.selected("code.grep", {"pattern": "needle", "path_prefix": "tests/"})
        self.assertFalse(result["request_dispatched"])
        self.backend.request.assert_not_called()

    def test_possible_prefixes_still_dispatch_and_require_authorization(self):
        self.backend.request.side_effect = PermissionError("Backend authorization denied")
        with self.assertRaisesRegex(PermissionError, "authorization denied"):
            self.context.selected("code.grep", {"pattern": "needle", "path_prefix": "src/"})
        self.backend.request.assert_called_once()
        retrievals = [e for e in self.ledger.rows if e["kind"] == "retrieval"]
        self.assertEqual(len(retrievals), 1)
        self.assertIn("authorization denied", retrievals[0]["error"])

    def test_local_scope_check_does_not_accept_forbidden_identity_arguments(self):
        with self.assertRaisesRegex(ValueError, "Unexpected retrieval argument"):
            self.context.selected(
                "code.grep", {"pattern": "needle", "path_prefix": "tests/", "namespace": "other"}
            )
        self.backend.request.assert_not_called()
        self.assertEqual(self.ledger.rows, [])

    def test_empty_and_partial_filename_prefixes_are_not_refused(self):
        calls = []
        original = ReplayContext(self.task)

        def request(op, body):
            calls.append(dict(body))
            receipt = original.request(op, body)
            receipt["data"] = {"matches": []}
            return receipt

        self.backend.request.side_effect = request
        for prefix in ("", "src", "src/", "src/subj"):
            result = self.context.selected(
                "code.grep", {"pattern": "needle", "path_prefix": prefix}
            )
            self.assertNotIn("server_coverage", result)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.context.requests, 4)

    def test_selected_calls_count_separately_from_undispatched_local_refusals(self):
        case, task = self.case, self.task

        class OutsideQueries(ReplayProvider):
            selected = False

            def step(self, messages, tools, max_tokens):
                if not self.selected:
                    self.selected = True
                    return {
                        "blocks": [
                            {
                                "toolUse": {
                                    "toolUseId": "outside-" + str(i),
                                    "name": name,
                                    "input": args,
                                }
                            }
                            for i, (name, args) in enumerate(
                                (
                                    ("loom_code_grep", {"pattern": "test", "path_prefix": "tests"}),
                                    (
                                        "loom_code_search",
                                        {"query": "test", "path_prefix": "tests/"},
                                    ),
                                )
                            )
                        ],
                        "usage": {},
                        "physical_requests": 0,
                        "cost_usd": 0,
                        "cost_status": "offline",
                    }
                return super().step(messages, tools, max_tokens)

        factory = Factory(
            task,
            OutsideQueries(ROOT / case["replay"]["path"]),
            ObjectiveGrader(ROOT / case["grader"]["path"], case["grader"]["sha256"]),
            self.root / "factory",
            mode="natural",
            backend=self.backend,
        )
        result = factory.run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["model_selected_loom_requests"], 2)
        self.assertEqual(result["model_selected_loom_dispatches"], 0)
        self.assertEqual(result["loom_logical_requests"], 0)
        self.assertEqual(result["loom_physical_requests"], 0)
        self.assertEqual(result["local_scope_refusals"], 2)
        self.assertEqual(result["loom_failed_requests"], 0)
        self.backend.request.assert_not_called()
