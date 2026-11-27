"""Source-only compiler commands cannot infer project identity from Git."""

import unittest
from pathlib import Path
from unittest.mock import patch


class CompilerRecipe(unittest.TestCase):
    def test_source_only_command_pins_the_public_base_as_project_version(self):
        from examples.software_factory.evals.flask.compiler import index_command

        with patch("subprocess.run", side_effect=AssertionError("No Git discovery")):
            command = index_command(
                Path("/compiler"), Path("/tools/scip-python"), base_commit="a" * 40
            )
        self.assertEqual(
            command,
            [
                "/tools/scip-python",
                "index",
                ".",
                "--project-name",
                "flask",
                "--project-version",
                "a" * 40,
                "--environment",
                "/compiler/environment.json",
                "--output",
                "/compiler/index.scip",
            ],
        )

    def test_a_full_base_is_required_instead_of_an_implicit_git_fallback(self):
        from examples.software_factory.evals.flask.compiler import index_command

        for commit in ["", "main", "a" * 12]:
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                index_command(Path("/compiler"), Path("/tools/scip-python"), base_commit=commit)
