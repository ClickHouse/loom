"""Every arm can follow source pages and sees the scope of its test feedback."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from examples.software_factory.factory.adapters import ReplayProvider
from examples.software_factory.factory.engine import Factory
from examples.software_factory.factory.model import digest
from examples.software_factory.tests.factory.test_factory import ROOT
from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader
from examples.software_factory.tests.fixtures.python.grading import load_task as load_fixture


class EvidencePages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        case = json.loads((ROOT / "suite.json").read_text())["tasks"]["name-validation"]
        task = load_fixture(ROOT / case["task"]["path"])
        path, original = next(iter(task.files.items()))
        files = {path: original + "".join(f"# needle {i}\n" for i in range(40)) + "#" * 17000}
        task = replace(
            task,
            files=files,
            source_sha256=digest(files),
            read_only_files={
                "docs/lifecycle.rst": "Public context teardown contract.\n",
                "tests/test_lifecycle.py": "def test_existing_behavior():\n    assert True\n",
            },
        )
        self.path = path
        self.factory = Factory(
            task,
            ReplayProvider(ROOT / case["replay"]["path"]),
            ObjectiveGrader(ROOT / case["grader"]["path"], case["grader"]["sha256"]),
            Path(self.temp.name) / "run",
            mode="baseline",
        )

    def test_local_search_discloses_and_follows_overflow(self):
        first = self.factory.execute("search", {"text": "needle"})
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 20)
        second = self.factory.execute("search", {"text": "needle", "offset": first["next_offset"]})
        self.assertEqual(len(first["matches"]), 20)
        self.assertEqual(len(second["matches"]), 20)
        self.assertFalse(second["has_more"])
        self.assertNotEqual(first["matches"][0], second["matches"][0])

    def test_ordinary_regex_search_is_available_to_the_baseline_with_paging(self):
        arguments = {"text": r"^# needle \d+$", "regex": True, "path_prefix": "src/"}
        first = self.factory.execute("search", arguments)
        second = self.factory.execute("search", {**arguments, "offset": first["next_offset"]})
        self.assertEqual(len(first["matches"]), 20)
        self.assertEqual(len(second["matches"]), 20)
        self.assertFalse(second["has_more"])
        self.assertEqual(self.factory.execute("search", {"text": arguments["text"]})["matches"], [])
        result = self.factory.execute(
            "search", {"text": r"^# needle (?:0|2[0-9])$", "regex": True, "path_prefix": "src/"}
        )
        self.assertEqual(len(result["matches"]), 11)
        docs = self.factory.execute(
            "search", {"text": "teardown|missing", "regex": True, "path_prefix": "docs/"}
        )
        self.assertEqual([row["path"] for row in docs["matches"]], ["docs/lifecycle.rst"])

    def test_ordinary_regex_refuses_unsupported_syntax_and_non_boolean_flags(self):
        import re2

        with self.assertRaises(re2.error):
            self.factory.execute("search", {"text": "(?<=needle) value", "regex": True})
        with self.assertRaises(ValueError):
            self.factory.execute("search", {"text": "needle", "regex": "false"})

    def test_partial_long_line_can_be_read_without_losing_characters(self):
        first = self.factory.execute("read", {"path": self.path})
        self.assertTrue(first["truncated"])
        second = self.factory.execute(
            "read",
            {
                "path": self.path,
                "start_line": first["next_start_line"],
                "start_column": first["next_start_column"],
            },
        )
        self.assertEqual(
            first["content"] + second["content"], self.factory.checkout.full_read(self.path)
        )
        self.assertFalse(second["truncated"])

    def test_available_tests_do_not_claim_task_completion(self):
        result = self.factory.execute("test", {})
        self.assertEqual(result["scope"], "fixed_existing_tests")
        self.assertFalse(result["task_completion_verified"])

    def test_public_evidence_can_be_discovered_read_and_searched(self):
        listing = self.factory.execute("list_files", {"prefix": "tests/"})
        self.assertEqual(listing["paths"], ["tests/test_lifecycle.py"])
        self.assertFalse(listing["has_more"])
        content = self.factory.execute("read", {"path": listing["paths"][0]})
        self.assertIn("test_existing_behavior", content["content"])
        hits = self.factory.execute("search", {"text": "teardown contract"})
        self.assertEqual(hits["matches"][0]["path"], "docs/lifecycle.rst")
        scoped = self.factory.execute(
            "search", {"text": "teardown contract", "path_prefix": "src/"}
        )
        self.assertEqual(scoped["matches"], [])

    def test_evidence_is_excluded_from_edits_and_candidate_artifacts(self):
        before = self.factory.checkout.state()
        with self.assertRaisesRegex(ValueError, "Only implementation"):
            self.factory.execute(
                "edit", {"path": "tests/test_lifecycle.py", "old": "True", "new": "False"}
            )
        self.assertEqual(self.factory.checkout.state(), before)
        self.assertEqual(set(self.factory.checkout.files()), {self.path})
        self.assertEqual(self.factory.checkout.diff(), "")
        for path in ("verifier/hidden.py", ".git/config", "../outside.py"):
            refused = self.factory.execute("read", {"path": path})
            self.assertTrue(refused["refused"])
            self.assertNotIn("content", refused)
            with self.assertRaises(PermissionError):
                self.factory.checkout.full_read(path)

    def test_listing_continuation_and_invalid_offsets(self):
        self.factory.checkout.read_only.update({f"docs/page-{i:03}.rst": "" for i in range(125)})
        first = self.factory.execute("list_files", {"prefix": "docs/page-"})
        self.assertTrue(first["has_more"])
        second = self.factory.execute(
            "list_files", {"prefix": "docs/page-", "offset": first["next_offset"]}
        )
        self.assertEqual(len(first["paths"] + second["paths"]), 125)
        self.assertFalse(second["has_more"])
        self.assertFalse(set(first["paths"]) & set(second["paths"]))
        for offset in (-1, True, "0"):
            with self.assertRaises(ValueError):
                self.factory.execute("list_files", {"offset": offset})
