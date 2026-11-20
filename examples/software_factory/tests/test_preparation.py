"""Authored receipt fixtures; no Docker, model, network or historical outcomes."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.behavioral import BehavioralGrader
from examples.software_factory.historical import (
    FrozenSource,
    HistoricalGrader,
    controller_hashes,
    git,
)
from examples.software_factory.preparation import prepare_behavioral, prepare_historical
from examples.software_factory.workspace import Checkout


def save(path, value):
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class HistoricalPreparationContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        mirror = self.root / "mirror"
        mirror.mkdir()
        git(mirror, "init", "--quiet")
        (mirror / "src/flask").mkdir(parents=True)
        (mirror / "src/flask/app.py").write_text("value = 'base'\n")
        git(mirror, "add", "src")
        git(
            mirror,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "base",
        )
        base = git(mirror, "rev-parse", "HEAD").decode().strip()
        (mirror / "src/flask/app.py").write_text("future_solution = 'hidden'\n")
        git(mirror, "add", "src")
        git(
            mirror,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "future",
        )
        private = {
            "base_commit": base,
            "FAIL_TO_PASS": json.dumps(["tests/a.py::test_regression"]),
            "PASS_TO_PASS": json.dumps(["tests/a.py::test_public"]),
            "patch": "REFERENCE_SOLUTION_SENTINEL",
            "test_patch": "HIDDEN_GRADER_SENTINEL",
        }
        task_path = self.root / "private.json"
        task_sha = save(task_path, private)
        self.config = self.root / "grader.json"
        selected = ["tests/a.py::test_regression", "tests/a.py::test_public"]
        config_sha = save(
            self.config,
            {
                "task_file": str(task_path),
                "task_sha256": task_sha,
                "source_git": str(mirror),
                "selected_tests": selected,
            },
        )
        image = "sha256:" + "a" * 64
        self.public = {
            "task_id": "authored-privacy-fixture",
            "repository": "pallets/flask",
            "base_commit": base,
            "problem": "repair the public behavior",
            "dependency_image": image,
            "grader_config_sha256": config_sha,
            "objective_selectors": selected,
            "prior_controls": {"visible-base": {"selected_tests": {selected[1]: "PASSED"}}},
        }
        source = FrozenSource(mirror, base)
        task = source.task(self.public, "unqualified-fixture", "value")
        grader = HistoricalGrader(source, image, [selected[1]], self.root / "grade")
        self.receipt = {
            "kind": "focused-historical-controls",
            "task_id": task.task_id,
            "base_commit": base,
            "dependency_image": image,
            "config_sha256": config_sha,
            "private_task_sha256": task_sha,
            "visible_selectors": [selected[1]],
            "hidden_selectors": selected,
            "qualified": True,
            "source_sha256": task.source_sha256,
            "controller_sha256": controller_hashes(),
            "controls": {
                name: {
                    "valid": True,
                    "passed": name != "base",
                    "grader_sha256": grader.sha256,
                    "selected": {
                        node: "FAILED" if name == "base" and node == selected[0] else "PASSED"
                        for node in ([selected[1]] if name == "visible-base" else selected)
                    },
                }
                for name in ("visible-base", "base", "reference")
            },
        }
        self.controls = self.root / "controls.json"
        self.sha = save(self.controls, self.receipt)

    def prepare(self, sha=None):
        with patch("examples.software_factory.qualify.public_task", return_value=self.public):
            return prepare_historical(
                self.public["task_id"],
                self.root / "worker",
                private_config=self.config,
                qualification=self.controls,
                qualification_sha256=sha or self.sha,
                namespace="unqualified-fixture",
                symbol="value",
            )

    def test_export_contains_only_base_source_and_private_grader_stays_outside_agent(self):
        task, grader = self.prepare()
        self.assertEqual(task.files, {"src/flask/app.py": "value = 'base'\n"})
        visible = json.dumps(vars(task))
        for hidden in ("future_solution", "REFERENCE_SOLUTION_SENTINEL", "HIDDEN_GRADER_SENTINEL"):
            self.assertNotIn(hidden, visible)
        checkout = Checkout(self.root / "agent", task)
        self.assertFalse((checkout.root / ".git").exists())
        with self.assertRaises(PermissionError):
            checkout.read("../private.json")
        self.assertTrue(grader.output.is_relative_to(self.root / "worker"))

    def test_passing_hidden_base_is_invalid_even_when_receipt_says_qualified(self):
        self.receipt["controls"]["base"]["selected"]["tests/a.py::test_regression"] = "PASSED"
        with self.assertRaisesRegex(PermissionError, "controls are invalid"):
            self.prepare(save(self.controls, self.receipt))

    def test_changed_grading_implementation_requires_new_controls(self):
        self.receipt["controller_sha256"]["historical.py"] = "0" * 64
        with self.assertRaisesRegex(PermissionError, "requires new controls"):
            self.prepare(save(self.controls, self.receipt))

    def test_modified_private_config_is_rejected(self):
        self.config.write_text(self.config.read_text() + " ")
        with self.assertRaisesRegex(PermissionError, "config differs"):
            self.prepare()

    def test_changed_receipt_bytes_require_the_exact_frozen_digest(self):
        self.controls.write_text(self.controls.read_text() + " ")
        with self.assertRaisesRegex(PermissionError, "receipt differs"):
            self.prepare()


class BehavioralPreparationContracts(unittest.TestCase):
    def setUp(self):
        HistoricalPreparationContracts.setUp(self)
        self.public["task_id"] = "pallets__flask-4045"
        config = json.loads(self.config.read_text())
        source = FrozenSource(Path(config["source_git"]), self.public["base_commit"])
        self.grader = BehavioralGrader(
            source,
            self.public["task_id"],
            self.public["dependency_image"],
            "a" * 64,
            self.root / "behavioral",
        )
        task = source.task(self.public, "unqualified-fixture", "value")
        controls = {}
        for name in ("visible-base", "base", "reference"):
            probes = [p for p in self.grader.probes if name != "visible-base" or p.visible]
            statuses = {p.key: name != "base" or p.visible for p in probes}
            controls[name] = {
                "passed": name != "base",
                "valid": True,
                "result_authority": "host",
                "grader_sha256": self.grader.sha256,
                "selected": statuses,
            }
        self.receipt = {
            "kind": "authored-host-behavioral-controls",
            "task_id": task.task_id,
            "base_commit": task.base_commit,
            "source_sha256": task.source_sha256,
            "dependency_image": self.public["dependency_image"],
            "config_sha256": self.public["grader_config_sha256"],
            "qualified": True,
            "coverage": "authored-public-behavior-not-official-pytest",
            "seed": "a" * 64,
            "grader_sha256": self.grader.sha256,
            "controls": controls,
        }
        self.sha = save(self.controls, self.receipt)

    def prepare(self, sha=None):
        with patch("examples.software_factory.preparation.public_task", return_value=self.public):
            return prepare_behavioral(
                self.public["task_id"],
                self.root / "worker",
                private_config=self.config,
                qualification=self.controls,
                qualification_sha256=sha or self.sha,
                namespace="unqualified-fixture",
                symbol="value",
            )

    def test_preparation_reads_no_private_task_and_exports_only_exact_base(self):
        (self.root / "private.json").unlink()
        task, grader = self.prepare()
        self.assertEqual(task.files, {"src/flask/app.py": "value = 'base'\n"})
        self.assertNotIn("seed", vars(task))
        self.assertEqual(grader.result_authority, "host")
        self.assertEqual(grader.sha256, self.grader.sha256)
        checkout = Checkout(self.root / "agent", task)
        self.assertFalse((checkout.root / ".git").exists())
        with self.assertRaises(PermissionError):
            checkout.read("../private.json")

    def test_forged_qualified_flag_does_not_accept_all_passing_base(self):
        row = self.receipt["controls"]["base"]
        row["selected"] = {k: True for k in row["selected"]}
        with self.assertRaisesRegex(PermissionError, "controls are invalid"):
            self.prepare(save(self.controls, self.receipt))

    def test_missing_observation_and_nonboolean_status_refuse(self):
        statuses = self.receipt["controls"]["reference"]["selected"]
        key = next(iter(statuses))
        statuses[key] = "true"
        with self.assertRaisesRegex(PermissionError, "controls are invalid"):
            self.prepare(save(self.controls, self.receipt))
        del statuses[key]
        with self.assertRaisesRegex(PermissionError, "controls are invalid"):
            self.prepare(save(self.controls, self.receipt))

    def test_changed_code_or_probe_seed_requires_new_controls(self):
        self.receipt["seed"] = "b" * 64
        with self.assertRaisesRegex(PermissionError, "requires new controls"):
            self.prepare(save(self.controls, self.receipt))


if __name__ == "__main__":
    unittest.main()
