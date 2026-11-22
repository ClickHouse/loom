"""Navigation metadata survives projection only for unchanged allowed source."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from examples.software_factory.factory.model import Budget, Ledger, Task, digest
from examples.software_factory.factory.retrieval_tools import QualifiedContext
from examples.software_factory.factory.workspace import Checkout


class ProjectionContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = "def resolve_name(value):\n    return value.strip()\n"
        self.path = "src/names.py"
        self.task = Task(
            "projection",
            "fixture",
            "example/repo",
            "a" * 40,
            "source",
            "Update name handling.",
            "resolve_name",
            {self.path: self.source},
            digest({self.path: self.source}),
        )
        self.calls = []
        owner = self

        class Backend:
            def request(self, op, body):
                owner.calls.append((op, body))
                return {
                    "data": owner.data,
                    "scope": {
                        "org": "fixture",
                        "namespace": owner.task.namespace,
                        "repository": owner.task.repository,
                        "commit": owner.task.base_commit,
                    },
                    "permission": "allowed",
                    "physical_requests": 1,
                }

        self.context = QualifiedContext(
            self.task,
            Checkout(self.root / "checkout", self.task),
            Backend(),
            Ledger(self.root / "events.jsonl"),
            Budget(),
            "fixture",
        )

    def definition(self, **extra):
        return {
            "path": self.path,
            "start_line": 1,
            "end_line": 2,
            "body": self.source,
            "unit_id": "name-unit",
            "symbol": "src.names::resolve_name",
            "signature": "(value)",
            "kind": "function",
            **extra,
        }

    def test_symbol_miss_keeps_scoped_candidates_without_another_backend_call(self):
        candidate = {"path": self.path, "qualified_name": "src.names::resolve_name"}
        self.data = {
            "definitions": [],
            "candidates": [
                candidate,
                {"path": "outside/private.py", "qualified_name": "outside::private"},
                {**candidate, "body": "not a candidate field"},
            ],
        }
        result = self.context.selected("code.symbol", {"name": "resolve_naem"})
        self.assertEqual(result["candidates"], [candidate])
        self.assertTrue(result["abstain"])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result["rejected"])

    def test_grounded_symbol_body_keeps_its_identity_and_navigation(self):
        result = self.context.project("code.symbol", {"definitions": [self.definition()]})
        unit = result["definitions"][0]
        self.assertEqual(unit["body"], self.source)
        self.assertEqual(unit["grounding"], "exact_local_span")
        self.assertEqual(unit["symbol"], "src.names::resolve_name")
        self.assertEqual(unit["unit_id"], "name-unit")
        self.assertEqual(unit["signature"], "(value)")

    def test_grounded_enclosing_body_keeps_the_resolved_name(self):
        item = self.definition(symbol_id="parser-unit", qualified_name="src.names::resolve_name")
        result = self.context.project("code.enclosing", {"results": [item]})
        self.assertEqual(result["definitions"][0]["qualified_name"], item["qualified_name"])
        self.assertEqual(result["definitions"][0]["symbol_id"], "parser-unit")

    def test_search_body_already_returned_is_grounded_without_refetch(self):
        self.data = {"results": [self.definition()]}
        result = self.context.selected("code.search", {"query": "normalize names"})
        self.assertEqual(result["definitions"][0]["body"], self.source)
        self.assertEqual(result["entries"][0]["unit_id"], "name-unit")
        self.assertEqual(len(self.calls), 1)

    def test_candidates_and_index_identity_are_removed_after_a_local_edit(self):
        self.context.checkout.edit(self.path, self.source, "# changed\n" + self.source)
        result = self.context.project(
            "code.symbol",
            {
                "definitions": [self.definition()],
                "candidates": [{"path": self.path, "qualified_name": "src.names::resolve_name"}],
            },
        )
        self.assertEqual(result["definitions"], [])
        self.assertEqual(result.get("candidates", []), [])
        self.assertTrue(result["rejected"])

    def test_elided_or_outside_bodies_cannot_be_lifted_as_definitions(self):
        for extra in ({"elided": True}, {"path": "outside/private.py"}, {"body": "wrong"}):
            with self.subTest(extra=extra):
                result = self.context.project(
                    "code.symbol",
                    {
                        "definitions": [self.definition(**extra)],
                    },
                )
                self.assertEqual(result["definitions"], [])
                self.assertTrue(result["rejected"])

    def test_unchanged_body_does_not_keep_index_identity_for_an_edited_file(self):
        self.context.checkout.edit(self.path, self.source, self.source + "# local edit\n")
        result = self.context.project("code.symbol", {"definitions": [self.definition()]})
        unit = result["definitions"][0]
        self.assertEqual(unit["body"], self.source)
        self.assertEqual(unit["grounding"], "exact_local_span")
        self.assertNotIn("unit_id", unit)
        self.assertNotIn("symbol", unit)
        self.assertTrue(result["rejected"])

    def test_invalid_candidate_span_cannot_become_a_navigation_pointer(self):
        result = self.context.project(
            "code.symbol",
            {
                "definitions": [],
                "candidates": [
                    {
                        "path": self.path,
                        "qualified_name": "src.names::resolve_name",
                        "start_line": 100,
                        "end_line": 200,
                    },
                ],
            },
        )
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["abstain"])
        self.assertTrue(result["rejected"])
