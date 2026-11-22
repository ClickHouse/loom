"""A nested source checkout applies patches independently of its parent repository."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class IsolatedPatchContracts(unittest.TestCase):
    def test_relative_and_absolute_source_roots_apply_the_same_patch(self):
        repository = Path(__file__).resolve().parents[4]
        for relative in (True, False):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(["git", "init", "-q", str(root)], check=True)
                workspace = root / "nested/workspace"
                (workspace / "tests").mkdir(parents=True)
                (workspace / "tests/probe.txt").write_text("before\n")
                script = (
                    "from pathlib import Path; import sys; "
                    "from examples.software_factory.evals.flask.historical import git; "
                    "patch=b'diff --git a/tests/probe.txt b/tests/probe.txt\\n"
                    "--- a/tests/probe.txt\\n+++ b/tests/probe.txt\\n"
                    "@@ -1 +1 @@\\n-before\\n+after\\n'; "
                    "git(Path(sys.argv[1]), 'apply', '-', data=patch)"
                )
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(workspace.relative_to(root) if relative else workspace),
                    ],
                    cwd=root,
                    env={**os.environ, "PYTHONPATH": str(repository)},
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual((workspace / "tests/probe.txt").read_text(), "after\n")
