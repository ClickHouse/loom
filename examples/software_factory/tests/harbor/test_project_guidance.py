"""Project instructions stay identical across treatments and preserve repository files."""

import hashlib
import tempfile
from pathlib import Path
from unittest import TestCase

from examples.software_factory.harbor.project_guidance import guidance, stage


class ProjectInstructions(TestCase):
    def test_project_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            existing = workspace / "CLAUDE.md"
            existing.write_text("Repository-specific instructions\n")
            with self.assertRaises(PermissionError):
                stage(workspace, guidance(["src"]))
            self.assertEqual(existing.read_text(), "Repository-specific instructions\n")

    def test_source_coverage_is_frozen_without_task_hints(self):
        instructions = guidance(["services", "crates"])
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            stage(workspace, instructions)
            actual = (workspace / "CLAUDE.md").read_bytes()
        self.assertEqual(hashlib.sha256(actual).hexdigest(), instructions["sha256"])
        self.assertIn("services, crates", actual.decode())
        self.assertIn("If no Loom server is configured", actual.decode())
