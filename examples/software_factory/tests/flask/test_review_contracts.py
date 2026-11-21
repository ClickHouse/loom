"""A refusal has no execution cost and a public run names both source revisions."""

import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.evals.flask.agent import VisibleGrader
from examples.software_factory.evals.flask.public_metadata import public_reproduction
from examples.software_factory.factory.model import TestRefusal
from examples.software_factory.harbor.public_export import public_metadata


class ReviewContracts(unittest.TestCase):
    def test_exhausted_visible_quota_is_a_predispatch_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            grader = VisibleGrader(
                None,
                None,
                {"grader_sha256": "a" * 64, "visible_test_call_limit": 1},
                Path(directory),
            )
            grader.calls = 1
            with self.assertRaises(TestRefusal):
                grader.run(None)
            self.assertEqual(grader.calls, 1)

    def test_controller_and_server_source_pins_are_distinct_and_complete(self):
        lock = json.loads(Path("examples/software_factory/evals/flask/suite.lock.json").read_text())
        protocol = {
            "server_commit": "a" * 40,
            "controller_source_identity": {
                "commit": "b" * 40,
                "dirty": False,
                "files": {"examples/software_factory/factory/engine.py": "c" * 64},
                "relative_files": {"examples/software_factory/factory/engine.py": "c" * 64},
            },
            "server_source_identity": {
                "files": {"server/main.py": "d" * 64},
                "dirty": True,
                "relative_files": {"server/main.py": "d" * 64},
            },
            "tasks": {row["task_id"]: {"image": "sha256:" + "e" * 64} for row in lock["tasks"]},
            "cost_basis": dict.fromkeys(
                (
                    "source",
                    "input_usd_per_million",
                    "output_usd_per_million",
                    "prompts_over_100000_input",
                    "prompts_over_100000_output",
                ),
                1,
            ),
        }
        implementation = public_reproduction(lock, protocol)["implementation"]
        self.assertEqual(implementation["controller_commit"], "b" * 40)
        self.assertEqual(implementation["server_commit"], "a" * 40)
        self.assertFalse(implementation["controller_dirty"])
        self.assertTrue(implementation["server_dirty"])
        self.assertEqual(
            implementation["controller_files"],
            protocol["controller_source_identity"]["files"],
        )
        self.assertEqual(
            implementation["server_files"], protocol["server_source_identity"]["files"]
        )
        public = public_reproduction(lock, protocol)
        public["pricing"]["source"] = "https://platform.claude.com/docs/en/about-claude/pricing"
        self.assertEqual(public_metadata(public)["implementation"], implementation)
        public["implementation"]["controller_files"] = {"/private/token.env": "a" * 64}
        with self.assertRaises(ValueError):
            public_metadata(public)
