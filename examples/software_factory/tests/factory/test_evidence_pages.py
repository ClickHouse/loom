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
        task = replace(task, files=files, source_sha256=digest(files))
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
