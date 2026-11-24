"""Loom guidance is staged outside the baseline checkout."""

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from examples.software_factory.harbor.project_guidance import SETTINGS, agent_image, guidance, stage


class ProjectInstructions(TestCase):
    def test_project_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            existing = workspace / "CLAUDE.md"
            existing.write_text("Repository-specific instructions\n")
            with self.assertRaises(PermissionError):
                stage(workspace, workspace / "loom-guidance", guidance(["src"]))
            self.assertEqual(existing.read_text(), "Repository-specific instructions\n")

    def test_source_coverage_is_frozen_without_task_hints(self):
        instructions = guidance(["services", "crates"])
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            actual = stage(workspace, workspace / "loom-guidance", instructions).read_bytes()
            self.assertFalse((workspace / "CLAUDE.md").exists())
            self.assertFalse((workspace / SETTINGS).exists())
            staged = json.loads((workspace / "loom-guidance" / SETTINGS).read_text())
            self.assertEqual(staged, instructions["native_settings"])
        self.assertEqual(hashlib.sha256(actual).hexdigest(), instructions["sha256"])
        self.assertIn("services, crates", actual.decode())
        self.assertEqual(instructions["applies_to"], "loom-enabled")
        self.assertIn("Use Loom to discover and read unchanged source", actual.decode())
        self.assertIn("use Loom for each search or read", actual.decode())
        self.assertIn("Shell grep and the Grep tool are", actual.decode())
        self.assertIn("Use local tools for edits, tests, Git", actual.decode())

    def test_baseline_and_loom_use_their_frozen_images(self):
        images = {"task": {"agent": {"baseline": "base-image", "loom": "loom-image"}}}
        self.assertEqual(agent_image(images, {"task_id": "task", "mode": "baseline"}), "base-image")
        self.assertEqual(agent_image(images, {"task_id": "task", "mode": "loom"}), "loom-image")
