"""Repository adapters keep language-specific execution outside shared code."""

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from examples.software_factory.evals.flask import profile
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.profiles import load_profile

ROOT = Path(__file__).resolve().parents[2]


class ProfileContracts(unittest.TestCase):
    def test_suite_is_repo_first_and_explicitly_selects_its_language_adapter(self):
        suite = json.loads((ROOT / "evals/flask/suite.lock.json").read_text())
        self.assertEqual(suite["languages"], ["python"])
        self.assertEqual(suite["source_roots"], ["src/flask", "tests", "docs"])
        self.assertEqual(suite["title"], "Python E2E evaluation: Flask")
        self.assertIs(load_profile({"suite": suite}), profile)
        with self.assertRaises(ValueError):
            load_profile({"suite": {"adapter": "unrelated.module"}})
        self.assertFalse((ROOT / "evals/python").exists())

    def test_shared_code_has_no_repo_or_smoke_fixture_import(self):
        for folder in ["factory", "harbor"]:
            for path in (ROOT / folder).glob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse(
                            (node.module or "").startswith(
                                (
                                    "examples.software_factory.evals.flask",
                                    "examples.software_factory.tests",
                                )
                            ),
                            path,
                        )

    def test_staged_python_grader_imports_without_the_host_source_tree(self):
        source_files = {"src/flask/a.py": "value = 1\n"}
        source = Mock()
        source.fixtures.return_value = {}
        task = SimpleNamespace(
            source_sha256=digest(source_files), read_only_files={}, read_only_roots=[]
        )
        config = dict(
            image="pinned-dependency",
            visible_selectors=["tests/test_a.py::test_a"],
            hidden_selectors=["tests/test_a.py::test_a"],
        )
        historical = SimpleNamespace(
            base={**source_files, "tests/test_a.py": "def test_a(): pass\n"}, sha256="grader"
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with (
                patch.object(
                    profile,
                    "task_inputs",
                    return_value=(config, source, task, {"test_patch": "patch"}),
                ),
                patch.object(profile, "HistoricalGrader", return_value=historical),
                patch.object(profile, "git"),
            ):
                spec, images = profile.stage_task(
                    {"visible_test_call_limit": None},
                    "public-task",
                    output,
                    lambda folder, image: "built-" + folder.name,
                )
            self.assertEqual(set(images), {"agent", "verifier"})
            self.assertEqual(spec["source_sha256"], task.source_sha256)
            for kind in ["agent", "verifier"]:
                files = output / "builds/public-task" / kind / "factory"
                self.assertTrue(
                    (files / "lib/examples/software_factory/factory/patterns.py").is_file()
                )
                script = (
                    "import runpy; d=runpy.run_path("
                    + repr(str(files / "grader.py"))
                    + "); assert callable(d['validate_candidate'])"
                )
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=output,
                    env={
                        "PATH": os.defpath,
                        "PYTHONPATH": str(files / "lib"),
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(result.returncode, 0)
                self.assertFalse((files / "lib/examples/software_factory/tests").exists())
