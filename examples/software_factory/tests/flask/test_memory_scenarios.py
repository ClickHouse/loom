"""Public history fixtures and private contracts stay separate and deterministic."""

import ast
import copy
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.evals.flask.memory import evaluation, fixtures
from examples.software_factory.factory.model import digest

SOURCES = {
    "src/flask/app.py": (
        "class Flask:\n"
        "    def make_response(self, rv, headers):\n"
        "        rv.headers.update(headers)\n"
        "        return rv\n"
    ),
    "src/flask/scaffold.py": (
        "class Scaffold:\n"
        "    def get_send_file_max_age(self, value):\n"
        "        if value is None:\n"
        "            return None\n"
        "        return value\n"
    ),
    "src/flask/json/__init__.py": (
        "def dumps(obj, app=None, **kwargs):\n"
        "    if app:\n"
        "        return app.json.dumps(obj, **kwargs)\n"
        "    return _json.dumps(obj, **kwargs)\n"
    ),
}


for _record in fixtures.load_project_memories():
    SOURCES.setdefault(_record["evidence"]["path"], "# public fixture source\n")


class SimulatedHistory(unittest.TestCase):
    def setUp(self):
        self.rows = copy.deepcopy(fixtures.load_project_memories())
        for row in self.rows:
            body = SOURCES[row["evidence"]["path"]]
            row["evidence"].update(
                anchor=body.splitlines()[0],
                line=1,
                source_sha256=hashlib.sha256(body.encode()).hexdigest(),
            )
        self.catalog_patcher = patch.object(
            fixtures, "load_project_memories", return_value=self.rows
        )
        self.catalog_patcher.start()
        self.addCleanup(self.catalog_patcher.stop)
        self.manifest = copy.deepcopy(fixtures.load_manifest())
        self.manifest["base_source_sha256"] = digest(SOURCES)
        self.case_ids = [case["case_id"] for case in self.manifest["cases"]]
        self.patcher = patch.object(fixtures, "load_manifest", return_value=self.manifest)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_deterministic_source_revisions_change_only_the_declared_function_file(self):
        original = copy.deepcopy(SOURCES)
        for case in self.manifest["cases"]:
            with self.subTest(case=case["case_id"]):
                result = fixtures.prepare_case(case["case_id"], SOURCES)
                self.assertEqual(result, fixtures.prepare_case(case["case_id"], SOURCES))
                self.assertEqual(json.loads(json.dumps(result)), result)
                self.assertEqual(result["current_revision"], "B")
                self.assertNotEqual(result["source_sha256"]["A"], result["source_sha256"]["B"])
                for revision, sources in result["source_revisions"].items():
                    self.assertEqual(set(sources), set(SOURCES))
                    self.assertEqual(digest(sources), result["source_sha256"][revision])
                    self.assertEqual(
                        [path for path in sources if sources[path] != SOURCES[path]],
                        [case["path"]],
                    )
                    for source in sources.values():
                        ast.parse(source)
        self.assertEqual(SOURCES, original)

    def test_overlays_exercise_three_different_public_maintenance_contracts(self):
        cases = {case_id: fixtures.prepare_case(case_id, SOURCES) for case_id in self.case_ids}
        header = cases["flask-memory-response-headers"]["source_revisions"]
        self.assertIn("rv.headers.extend(headers)", header["A"]["src/flask/app.py"])
        self.assertIn("rv.headers.update(dict(headers))", header["B"]["src/flask/app.py"])
        cache = cases["flask-memory-cache-policy"]["source_revisions"]
        self.assertIn("return 43200", cache["A"]["src/flask/scaffold.py"])
        self.assertIn("if not value:", cache["B"]["src/flask/scaffold.py"])
        js = cases["flask-memory-json-context"]["source_revisions"]
        self.assertIn("return _json.dumps(obj", js["A"]["src/flask/json/__init__.py"])
        self.assertIn("_remembered_options", js["B"]["src/flask/json/__init__.py"])

    def test_every_native_operation_has_the_same_facts_in_ordinary_public_documents(self):
        for case_id in self.case_ids:
            result = fixtures.prepare_case(case_id, SOURCES)
            events, documents = result["history_events"], result["public_documents"]
            count = len(self.rows)
            updates = result["history_counts"]["revision_updates"]
            self.assertEqual(len(events), count + updates)
            self.assertEqual(len(documents), count + updates)
            self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
            self.assertEqual(len({e["key"] for e in events}), count)
            for event in events:
                matching = [
                    document
                    for document in documents.values()
                    if event["value"] in document and event["key"] in document
                ]
                self.assertEqual(len(matching), 1)
                self.assertIn("Logical revision: " + event["revision"], matching[0])
                for path in event["files"]:
                    self.assertIn(path, matching[0])
                self.assertNotIn("created_at", event)
            current = {event["key"]: event for event in events}
            self.assertEqual(len([name for name in documents if "/current/" in name]), len(current))
            self.assertNotIn("project-history/current.md", documents)
            self.assertEqual(result["history_events_sha256"], digest(events))
            self.assertEqual(result["public_documents_sha256"], digest(documents))
            self.assertEqual(
                result["history_counts"],
                dict(
                    current_rows=100,
                    code_linked_rows=40,
                    unlinked_rows=60,
                    setup_operations=103,
                    revision_updates=3,
                ),
            )

    def test_revision_updates_reuse_existing_code_linked_keys(self):
        for case_id in self.case_ids:
            events = fixtures.prepare_case(case_id, SOURCES)["history_events"]
            values = {}
            updates = []
            for event in events:
                if event["operation"] == "update":
                    self.assertIn(event["key"], values)
                    self.assertNotEqual(event["value"], values[event["key"]])
                    self.assertTrue(event["files"])
                    self.assertEqual(event["revision"], "B")
                    self.assertEqual(event["precondition"], "native content hash from prior get")
                    updates.append(event)
                else:
                    self.assertNotIn(event["key"], values)
                values[event["key"]] = event["value"]
            self.assertEqual(len(updates), 3)
            self.assertEqual(len(values), len(self.rows))

    def test_public_payload_contains_no_private_evaluator_or_reference_answer(self):
        for case_id in self.case_ids:
            result = fixtures.prepare_case(case_id, SOURCES)
            public = json.dumps(result)
            for forbidden in ("test_patch", "hidden_selectors", "reference_sources", "assert "):
                self.assertNotIn(forbidden, public)
            self.assertIn("simulated", result["simulation"])
            self.assertNotIn("Loom", result["problem"])
            self.assertEqual(result["dependency_task_id"], "pallets__flask-4935")

    def test_past_memory_does_not_contain_the_future_issue_or_benchmark_instructions(self):
        for case in self.manifest["cases"]:
            events = fixtures.prepare_case(case["case_id"], SOURCES)["history_events"]
            values = [event["value"] for event in events]
            self.assertEqual(len(set(values)), len(events))
            for event in events:
                self.assertNotIn(case["problem"], event["value"])
                self.assertLessEqual(len(event["value"].split()), 60)
                self.assertLessEqual(len(event["value"]), 600)
                if event["operation"] == "update":
                    self.assertEqual(event["files"], [case["path"]])
                for excluded in ("simulat", "benchmark", "Loom", "historical timestamp"):
                    self.assertNotIn(excluded, event["value"])
            affected = [e for e in events if case["path"] in e["files"] and e["operation"] == "set"]
            self.assertEqual(len(affected), 3)

    def test_private_checks_are_executable_pytest_functions_and_an_add_file_patch(self):
        for case_id in self.case_ids:
            spec = evaluation.evaluator_spec(case_id)
            tree = ast.parse(spec["test_source"])
            functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
            self.assertEqual(len(functions), 2)
            self.assertEqual(
                spec["hidden_selectors"],
                [spec["test_path"] + "::" + name for name in functions],
            )
            self.assertTrue(spec["test_patch"].startswith("--- /dev/null\n+++ b/tests/"))
            self.assertIn("\n+def test_", spec["test_patch"])
            self.assertEqual(spec["visibility"], "evaluator-private")
            compile(spec["test_source"], spec["test_path"], "exec")

    def test_reference_control_is_original_frozen_source_and_is_not_aliased(self):
        for case_id in self.case_ids:
            reference = evaluation.reference_sources(case_id, SOURCES)
            self.assertEqual(reference, SOURCES)
            self.assertIsNot(reference, SOURCES)
            reference["src/flask/app.py"] = "changed"
            self.assertNotEqual(reference, SOURCES)

    def test_wrong_source_identity_unknown_case_and_wrong_anchor_refuse(self):
        for operation in (fixtures.prepare_case, evaluation.reference_sources):
            with self.assertRaises(PermissionError):
                operation(self.case_ids[0], {**SOURCES, "extra": "untrusted"})
            with self.assertRaises(ValueError):
                operation("unknown-case", SOURCES)
        with self.assertRaises(ValueError):
            evaluation.evaluator_spec("unknown-case")
        changed = {**SOURCES, "src/flask/app.py": "class Different:\n    pass\n"}
        self.manifest["base_source_sha256"] = digest(changed)
        with self.assertRaisesRegex(PermissionError, "Pinned class shape"):
            fixtures.prepare_case(self.case_ids[0], changed)

    def test_overlay_refuses_duplicate_target_instead_of_guessing(self):
        changed = dict(SOURCES)
        changed["src/flask/app.py"] += "        rv.headers.update(headers)\n"
        self.manifest["base_source_sha256"] = digest(changed)
        with self.assertRaisesRegex(PermissionError, "exactly once"):
            fixtures.prepare_case(self.case_ids[0], changed)

    def test_wrong_source_evidence_and_long_obsolete_fact_refuse(self):
        self.rows[0]["evidence"]["line"] = 99999
        with self.assertRaisesRegex(PermissionError, "evidence"):
            fixtures.prepare_case(self.case_ids[0], SOURCES)
        self.rows[0]["evidence"]["line"] = 1
        self.manifest["cases"][0]["history_revisions"][0]["value_a"] = "word " * 61
        with self.assertRaisesRegex(PermissionError, "short"):
            fixtures.prepare_case(self.case_ids[0], SOURCES)

    def test_public_manifest_declares_only_implementation_paths(self):
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(len(set(self.case_ids)), 3)
        self.assertEqual(len(self.manifest["base_commit"]), 40)
        for case in self.manifest["cases"]:
            self.assertTrue(case["path"].startswith("src/flask/"))
            self.assertNotIn("test", case["path"])
            self.assertNotIn("assert ", json.dumps(case))


class ProjectMemoryCatalog(unittest.TestCase):
    def test_unlinked_fact_bodies_use_plain_prose_without_code_shaped_names(self):
        code_shape = re.compile(r"\b[a-z]\w*_\w+\b|[a-z][A-Z]|`|::|[A-Za-z_]\w*\(\)")
        for row in fixtures.load_project_memories():
            if not row["files"]:
                self.assertIsNone(code_shape.search(row["value"]), row["key"])

    def test_exactly_100_short_distinct_facts_have_40_links_and_60_explicit_nonlinks(self):
        rows = fixtures.load_project_memories()
        self.assertEqual(len(rows), 100)
        self.assertEqual(sum(bool(row["files"]) for row in rows), 40)
        self.assertEqual(sum(row["files"] == [] for row in rows), 60)
        for row in rows:
            self.assertLessEqual(len(row["value"].split()), 60)
            self.assertLessEqual(len(row["value"]), 600)
            self.assertTrue(row["evidence"]["anchor"])
            self.assertEqual(len(row["evidence"]["source_sha256"]), 64)
            self.assertTrue(row["evidence"]["path"].startswith("src/flask/"))
        self.assertEqual(len({row["key"] for row in rows}), 100)
        self.assertEqual(len({row["value"] for row in rows}), 100)

    def test_oversized_fact_or_wrong_link_counts_refuse_before_preparation(self):
        original = json.loads(fixtures.MEMORIES.read_text())
        for mutate in ("word_count", "character_count", "links", "duplicate_key"):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as tmp:
                catalog = copy.deepcopy(original)
                if mutate == "word_count":
                    catalog["records"][0]["value"] = "word " * 61
                elif mutate == "character_count":
                    catalog["records"][0]["value"] = "x" * 601
                elif mutate == "links":
                    catalog["records"][0]["files"] = []
                else:
                    catalog["records"][0]["key"] = catalog["records"][1]["key"]
                path = Path(tmp) / "catalog.json"
                path.write_text(json.dumps(catalog))
                with patch.object(fixtures, "MEMORIES", path), self.assertRaises(PermissionError):
                    fixtures.load_project_memories()
