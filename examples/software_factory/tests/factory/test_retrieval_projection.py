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

    def test_grep_keeps_verified_scope_ranges_for_both_function_and_unscoped_hits(self):
        hit = dict(
            path=self.path,
            line=1,
            text="def resolve_name(value):",
            scope="src.names::resolve_name",
            scope_start_line=1,
            scope_end_line=2,
        )
        result = self.context.project("code.grep", {"matches": [hit]})
        self.assertEqual(result["matches"], [hit])
        self.assertEqual(result["rejected"], [])
        plain = {**hit, "scope": "", "scope_start_line": 0, "scope_end_line": 0}
        self.assertEqual(
            self.context.project("code.grep", {"matches": [plain]})["matches"],
            [{key: plain[key] for key in ("path", "line", "text", "scope")}],
        )

    def test_grep_keeps_source_line_but_withholds_stale_or_invalid_scope(self):
        hit = dict(
            path=self.path,
            line=1,
            text="def resolve_name(value):",
            scope="src.names::resolve_name",
            scope_start_line=2,
            scope_end_line=2,
        )
        result = self.context.project("code.grep", {"matches": [hit]})
        expected = {key: hit[key] for key in ("path", "line", "text")}
        self.assertEqual(result["matches"], [expected])
        self.assertTrue(result["rejected"])
        self.context.checkout.edit(self.path, "return value.strip()", "return value")
        hit["scope_start_line"] = 1
        result = self.context.project("code.grep", {"matches": [hit]})
        self.assertEqual(result["matches"], [expected])
        self.assertEqual(result["rejected"], [])
        result = self.context.project("code.grep", {"matches": [{**hit, "path": "docs/notes.rst"}]})
        self.assertEqual(result["matches"], [])
        self.assertTrue(result["rejected"])

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

    def test_elided_definitions_keep_scoped_navigation_without_claiming_a_body(self):
        for op, field in [("code.symbol", "definitions"), ("code.enclosing", "results")]:
            with self.subTest(op=op):
                item = self.definition(
                    body="", elided=True, next='code.symbol(symbol_id="name-unit")'
                )
                result = self.context.project(op, {field: [item, item]})
                self.assertEqual(result["definitions"], [])
                self.assertFalse(result["abstain"])
                self.assertTrue(result["navigation_only"])
                self.assertEqual(len(result["candidates"]), 1)
                card = result["candidates"][0]
                self.assertEqual(card["unit_id"], "name-unit")
                self.assertEqual(card["next"], item["next"])
                self.assertTrue(card["elided"])
                self.assertNotIn("body", card)
        self.context.checkout.edit(self.path, self.source, "# changed\n" + self.source)
        result = self.context.project("code.symbol", {"definitions": [item]})
        self.assertEqual(result.get("candidates", []), [])
        self.assertTrue(result["rejected"])

    def test_a_native_symbol_identifier_can_be_followed_without_a_name(self):
        self.data = {"definitions": [self.definition()]}
        result = self.context.selected("code.symbol", {"symbol_id": "name-unit"})
        self.assertEqual(result["definitions"][0]["body"], self.source)
        self.assertEqual(self.calls[0][1]["symbol_id"], "name-unit")
        self.assertNotIn("name", self.calls[0][1])
        self.assertNotIn("token_budget", self.calls[0][1])

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

    def test_skeleton_keeps_verified_outline_as_navigation(self):
        child = {"qualified_name": "resolve_name", "start_line": 1, "end_line": 2}
        item = self.definition(
            body="def resolve_name(value): ...",
            granularity="skeleton",
            outline=[child],
            description="Source summary",
        )
        result = self.context.project("code.symbol", {"definitions": [item]})
        self.assertFalse(result["abstain"])
        self.assertTrue(result["navigation_only"])
        self.assertEqual(result["definitions"], [])
        card = result["candidates"][0]
        self.assertEqual(card["description"], "Source summary")
        self.assertEqual(card["granularity"], "skeleton")
        self.assertEqual(card["outline"], [{"path": self.path, **child}])
        self.assertNotIn("body", card)
        invalid = {**item, "outline": [{**child, "end_line": 3}]}
        refused = self.context.project("code.symbol", {"definitions": [invalid]})
        self.assertNotIn("candidates", refused)
        self.assertTrue(refused["abstain"])
        self.assertTrue(refused["rejected"])

    def test_successful_projection_retains_raw_native_response_in_private_ledger(self):
        self.data = {"definitions": [self.definition()]}
        result = self.context.selected("code.symbol", {"name": "resolve_name"})
        native = [row for row in self.context.ledger.rows if row["kind"] == "native_context"]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0]["response"], self.data)
        self.assertEqual(native[0]["response_sha256"], digest(self.data))
        self.assertNotIn("response", result)

    def test_navigation_keeps_its_bound_historical_lessons_without_a_body(self):
        self.context.memory_namespaces = ("history",)
        self.data = {
            "definitions": [
                self.definition(
                    body="",
                    elided=True,
                    lessons=[
                        {
                            "namespace": "history",
                            "memory_key": "compatibility",
                            "excerpt": "Preserve old callers.",
                        }
                    ],
                )
            ]
        }
        result = self.context.project("code.symbol", self.data)
        self.assertEqual(result["definitions"], [])
        self.assertEqual(result["candidates"][0]["lessons"], self.data["definitions"][0]["lessons"])
        self.data["definitions"][0]["lessons"][0]["namespace"] = "outside"
        with self.assertRaises(PermissionError):
            self.context.project("code.symbol", self.data)

    def test_search_keeps_source_after_a_retrieval_context_prefix(self):
        for prefix in (
            "A function summary.\n",
            "A multi-line\nsource comment.\n",
            "# … continued from resolve_name(value)\n",
        ):
            with self.subTest(prefix=prefix):
                result = self.context.project(
                    "code.search", {"results": [self.definition(body=prefix + self.source)]}
                )
                self.assertEqual(result["rejected"], [])
                self.assertEqual(result["definitions"][0]["body"], self.source)
                self.assertEqual(result["definitions"][0]["retrieval_context"], prefix)
                self.assertEqual(result["definitions"][0]["grounding"], "exact_local_span")

    def test_a_prefix_cannot_rescue_wrong_truncated_or_outside_source(self):
        for extra in (
            {"body": "Summary\n" + self.source.replace(".strip()", ".lower()")},
            {"body": "Summary\n" + self.source.splitlines()[0]},
            {"path": "outside/private.py", "body": "Summary\n" + self.source},
            {"start_line": 3, "end_line": 5, "body": "Summary\n" + self.source},
        ):
            with self.subTest(extra=extra):
                result = self.context.project(
                    "code.search", {"results": [self.definition(**extra)]}
                )
                self.assertEqual(result.get("definitions", []), [])
                self.assertTrue(result["rejected"])
        strict = self.context.project(
            "code.symbol", {"definitions": [self.definition(body="Summary\n" + self.source)]}
        )
        self.assertEqual(strict["definitions"], [])
        self.assertTrue(strict["rejected"])

    def test_verified_source_does_not_keep_a_prefix_from_an_edited_file(self):
        self.context.checkout.edit(self.path, self.source, self.source + "# local change\n")
        result = self.context.project(
            "code.search", {"results": [self.definition(body="Summary\n" + self.source)]}
        )
        self.assertEqual(result["definitions"][0]["body"], self.source)
        self.assertNotIn("retrieval_context", result["definitions"][0])
        self.assertNotIn("unit_id", result["definitions"][0])
