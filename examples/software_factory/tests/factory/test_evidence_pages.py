"""Every arm can follow source pages and sees the scope of its test feedback."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

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
        with self.assertRaisesRegex(ValueError, "Invalid RE2 pattern"):
            self.factory.execute("search", {"text": "(?<=needle) value", "regex": True})
        with self.assertRaises(ValueError):
            self.factory.execute("search", {"text": "needle", "regex": "false"})

    def test_unknown_tool_is_refused_without_stopping_the_remaining_batch(self):
        reply = {
            "blocks": [
                {
                    "toolUse": {
                        "toolUseId": "unknown",
                        "name": "bash",
                        "input": {"cmd": 'grep -n "filterwarnings" -A5 setup.cfg tox.ini'},
                    }
                },
                {"toolUse": {"toolUseId": "listed", "name": "list_files", "input": {}}},
            ],
            "usage": {},
            "physical_requests": 0,
            "cost_usd": 0,
        }
        state = self.factory.checkout.state()
        with patch.object(self.factory.provider, "step", return_value=reply):
            self.assertTrue(self.factory.turn(1))
        rows = [json.loads(line) for line in self.factory.ledger.path.read_text().splitlines()]
        results = [row["result"] for row in rows if row["kind"] == "tool_result"]
        self.assertIn("Unavailable tool", results[0]["error"])
        self.assertIn(self.path, results[1]["paths"])
        self.assertEqual(self.factory.checkout.state(), state)
        self.assertFalse(any(row["kind"] == "tool_failure" for row in rows))

    def test_authorization_failure_from_an_available_tool_still_stops_the_attempt(self):
        reply = {
            "blocks": [{"toolUse": {"toolUseId": "listed", "name": "list_files", "input": {}}}],
            "usage": {},
            "physical_requests": 0,
            "cost_usd": 0,
        }
        with (
            patch.object(self.factory.provider, "step", return_value=reply),
            patch.object(self.factory, "execute", side_effect=PermissionError("Scope changed")),
            self.assertRaises(PermissionError),
        ):
            self.factory.turn(1)
        rows = [json.loads(line) for line in self.factory.ledger.path.read_text().splitlines()]
        self.assertEqual(
            [r["error_type"] for r in rows if r["kind"] == "tool_failure"], ["PermissionError"]
        )
        self.assertFalse(any(row["kind"] == "tool_result" for row in rows))

    def test_invalid_regex_is_a_tool_error_and_the_remaining_batch_runs(self):
        class Provider:
            label = "offline-replay"
            calls = 0

            def step(self, *args):
                self.calls += 1
                blocks = (
                    [
                        {
                            "toolUse": {
                                "toolUseId": "bad",
                                "name": "search",
                                "input": {
                                    "text": "_blueprint_order|_get_blueprint_order|reversed(",
                                    "regex": True,
                                },
                            }
                        },
                        {
                            "toolUse": {
                                "toolUseId": "good",
                                "name": "search",
                                "input": {
                                    "text": "needle",
                                },
                            }
                        },
                    ]
                    if self.calls == 1
                    else []
                )
                return dict(blocks=blocks, usage={}, physical_requests=0, cost_usd=0)

        self.factory.provider = Provider()
        result = self.factory.run()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completion_reviews"], 1)
        self.assertEqual(result["tool_failed_requests"], 1)
        rows = [json.loads(line) for line in self.factory.ledger.path.read_text().splitlines()]
        results = [row["result"] for row in rows if row["kind"] == "tool_result"]
        self.assertIn("Invalid RE2 pattern", results[0]["error"])
        self.assertEqual(len(results[1]["matches"]), 20)

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

    def test_explicit_line_range_stops_without_reading_the_following_source(self):
        result = self.factory.execute("read", {"path": self.path, "start_line": 2, "end_line": 3})
        expected = "".join(self.factory.checkout.full_read(self.path).splitlines(True)[1:3])
        self.assertEqual(result["content"], expected)
        self.assertFalse(result["truncated"])
        self.assertIsNone(result["next_start_line"])
        for end in (0, 1, True, "3"):
            with self.subTest(end=end), self.assertRaises(ValueError):
                self.factory.execute("read", {"path": self.path, "start_line": 2, "end_line": end})

    def test_line_range_retains_long_line_continuation(self):
        source = self.factory.checkout.full_read(self.path)
        line = len(source.splitlines())
        first = self.factory.execute(
            "read", {"path": self.path, "start_line": line, "end_line": line}
        )
        self.assertTrue(first["truncated"])
        second = self.factory.execute(
            "read",
            {
                "path": self.path,
                "start_line": first["next_start_line"],
                "start_column": first["next_start_column"],
                "end_line": line,
            },
        )
        self.assertEqual(first["content"] + second["content"], source.splitlines(True)[-1])
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
