from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.factory.adapters import ReplayContext, ReplayProvider
from examples.software_factory.factory.engine import Factory
from examples.software_factory.factory.model import Budget, Ledger
from examples.software_factory.factory.retrieval_tools import TOOL_OPS, QualifiedContext
from examples.software_factory.factory.workspace import Checkout
from examples.software_factory.tests.factory.test_factory import ROOT
from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader
from examples.software_factory.tests.fixtures.python.grading import load_task as load_fixture


class RetrievalTools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = json.loads((ROOT / "suite.json").read_text())["tasks"]["name-validation"]
        self.task = load_fixture(ROOT / self.case["task"]["path"])
        self.context = QualifiedContext(
            self.task,
            Checkout(self.root / "checkout", self.task),
            ReplayContext(self.task),
            Ledger(self.root / "events.jsonl"),
            Budget(),
            "fixture",
        )

    def test_code_reads_leave_token_and_search_page_budgets_to_the_serving_api(self):
        requests = []
        original = self.context.backend.request

        def record(op, body):
            requests.append((op, dict(body)))
            return original(op, body)

        self.context.backend.request = record
        path = next(iter(self.task.files))
        for op, args in [
            ("code.symbol", {"name": self.task.symbol}),
            ("code.search", {"query": "public concept"}),
            ("code.outline", {"path": path}),
            ("code.enclosing", {"locations": [path + ":1"]}),
        ]:
            self.context.selected(op, args)
        self.assertEqual(len(requests), 4)
        for op, body in requests:
            with self.subTest(op=op):
                self.assertNotIn("token_budget", body)
                self.assertNotIn("top_k", body)
        search = next(body for op, body in requests if op == "code.search")
        self.assertEqual(search["legs"], "lexical")
        self.assertEqual(search["llm_legs"], "none")
        self.assertEqual(search["bodies"], "none")
        symbol = requests[0][1]
        self.assertFalse(symbol["include_history"])
        self.assertFalse(symbol["include_examples"])
        self.assertFalse(symbol["include_lessons"])

    def test_search_cursor_continues_the_server_default_page_without_an_override(self):
        requests = []
        task = self.task

        class Paged(ReplayContext):
            def request(self, op, body):
                requests.append(dict(body))
                receipt = super().request(op, body)
                receipt["data"] = {"results": [], "has_more": True, "next_cursor": "page-two"}
                return receipt

        self.context.backend = Paged(task)
        result = self.context.selected("code.search", {"query": "concept", "cursor": "page-one"})
        self.context.selected("code.search", {"query": "concept", "cursor": result["next_cursor"]})
        self.assertEqual([r["cursor"] for r in requests], ["page-one", "page-two"])
        self.assertTrue(result["has_more"])
        self.assertTrue(all("top_k" not in r and "token_budget" not in r for r in requests))

    def test_attempt_budget_has_no_silently_ignored_symbol_token_setting(self):
        from dataclasses import asdict

        self.assertNotIn("symbol_tokens", asdict(Budget()))

    def test_only_explicit_model_reads_can_dispatch(self):
        self.assertFalse(hasattr(self.context, "lookup"))
        self.assertEqual(self.context.requests, 0)

    def test_compiler_tier_provenance_survives_only_with_locally_grounded_source(self):
        path, source = next(iter(self.task.files.items()))
        item = dict(
            path=path,
            start_line=1,
            end_line=len(source.splitlines()),
            body=source,
            tier="scip",
            confidence=1.0,
        )
        result = self.context.project("code.symbol", {"definitions": [item]})
        self.assertEqual(result["definitions"][0]["tier"], "scip")
        self.assertEqual(result["definitions"][0]["confidence"], 1.0)
        rejected = self.context.project(
            "code.symbol", {"definitions": [{**item, "path": "tests/private.py"}]}
        )
        self.assertEqual(rejected["definitions"], [])
        self.assertTrue(rejected["rejected"])

    def test_repeated_enclosing_definitions_are_returned_once(self):
        path, source = next(iter(self.task.files.items()))
        item = dict(path=path, start_line=1, end_line=len(source.splitlines()), body=source)
        result = self.context.project("code.enclosing", {"results": [item, item]})
        self.assertEqual(len(result["definitions"]), 1)

    def test_grep_page_can_be_followed_without_changing_scope(self):
        calls = []
        task = self.task

        class Paged(ReplayContext):
            def request(self, op, body):
                calls.append(dict(body))
                receipt = super().request(op, body)
                receipt["data"] = {"matches": [], "has_more": True, "next_cursor": "next-page"}
                return receipt

        self.context.backend = Paged(task)
        result = self.context.selected("code.grep", {"pattern": "needle", "cursor": "page-one"})
        self.assertEqual(calls[0]["cursor"], "page-one")
        self.assertEqual(calls[0]["org"], "fixture")
        self.assertEqual(calls[0]["namespace"], task.namespace)
        self.assertEqual(result["next_cursor"], "next-page")

    def test_bad_line_and_stale_navigation_are_rejected(self):
        path, source = next(iter(self.task.files.items()))
        result = self.context.project(
            "code.grep",
            {
                "partial": True,
                "matches": [
                    dict(path=path, line=1, text="incorrect pointer"),
                ],
            },
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["rejected"]), 1)
        self.context.checkout.edit(path, source, "# new\n" + source)
        result = self.context.project("code.search", {"results": [dict(path=path)]})
        self.assertEqual(result["entries"], [])
        self.assertEqual(len(result["rejected"]), 1)

    def test_identity_and_provider_arguments_cannot_be_selected(self):
        for op, args in [("memory.set", {}), ("code.search", {"org": "other"})]:
            with self.subTest(op=op), self.assertRaises((ValueError, PermissionError)):
                self.context.selected(op, args)
        self.assertEqual(self.context.requests, 0)

    def test_model_choices_are_counted_including_cached_named_tools(self):
        case = self.case
        task = self.task

        class Select(ReplayProvider):
            chosen = False

            def step(self, messages, tools, max_tokens):
                if not self.chosen:
                    self.chosen = True
                    return dict(
                        blocks=[
                            {
                                "toolUse": dict(
                                    toolUseId=str(i),
                                    name="loom_code_symbol",
                                    input={"name": task.symbol},
                                )
                            }
                            for i in range(2)
                        ],
                        usage={},
                        physical_requests=0,
                        cost_usd=0,
                        cost_status="offline",
                    )
                return super().step(messages, tools, max_tokens)

        grader = ObjectiveGrader(ROOT / case["grader"]["path"], case["grader"]["sha256"])
        factory = Factory(
            task,
            Select(ROOT / case["replay"]["path"]),
            grader,
            self.root / "run",
            mode="natural",
            backend=ReplayContext(task),
        )
        result = factory.run()
        self.assertEqual(result["model_selected_loom_requests"], 2)
        self.assertEqual(result["model_selected_loom_dispatches"], 1)
        self.assertEqual(result["memory_rows_written"], 0)
        self.assertTrue(set(TOOL_OPS) <= {t["name"] for t in factory.tools})

    def test_baseline_has_no_backend_and_optional_loom_has_no_host_prefetch(self):
        grader = ObjectiveGrader(ROOT / self.case["grader"]["path"], self.case["grader"]["sha256"])
        factory = Factory(
            self.task,
            ReplayProvider(ROOT / self.case["replay"]["path"]),
            grader,
            self.root / "run",
            mode="natural",
            backend=ReplayContext(self.task),
        )
        result = factory.run()
        self.assertEqual(result["loom_logical_requests"], 0)
        self.assertTrue(result["resolved"])
        baseline = Factory(
            self.task,
            ReplayProvider(ROOT / self.case["replay"]["path"]),
            grader,
            self.root / "baseline",
            mode="baseline",
        )
        self.assertIsNone(baseline.context)
        self.assertFalse(set(TOOL_OPS) & {t["name"] for t in baseline.tools})
        self.assertEqual(factory.messages[0]["content"][0], baseline.messages[0]["content"][0])
