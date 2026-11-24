"""Loom guidance is staged outside the baseline checkout."""

import hashlib
import tempfile
from pathlib import Path
from unittest import TestCase

from examples.software_factory.harbor.project_guidance import (
    agent_image,
    guidance,
    stage,
    task_instruction,
)


class ProjectInstructions(TestCase):
    def test_submission_scope_preserves_the_original_issue(self):
        issue = "Example issue\r\nOriginal body\r\n"
        rendered = task_instruction(issue)
        self.assertTrue(rendered.startswith(issue + "\n\n## Submission\n\n"))
        self.assertIn("Keep the patch to runtime source and tests.", rendered)

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
        self.assertEqual(hashlib.sha256(actual).hexdigest(), instructions["sha256"])
        self.assertIn("services, crates", actual.decode())
        self.assertEqual(instructions["applies_to"], "loom-enabled")
        text = " ".join(actual.decode().split())
        self.assertIn("Use Loom for each search or read of unchanged indexed source", text)
        self.assertIn("including test files and module imports", text)
        self.assertIn(
            "start with `loom_code_edit_context` for its implementation, "
            "related tests, and linked notes",
            text,
        )
        self.assertIn("or literal code fragments as `clues`", text)
        self.assertIn("With clues, pass the file's path as `name` if known", text)
        self.assertIn("Keep discovery separate from edits, test execution and Git", text)
        self.assertIn("read working-copy changes, untracked or unindexed files", text)

    def test_baseline_and_loom_use_their_frozen_images(self):
        images = {"task": {"agent": {"baseline": "base-image", "loom": "loom-image"}}}
        self.assertEqual(agent_image(images, {"task_id": "task", "mode": "baseline"}), "base-image")
        self.assertEqual(agent_image(images, {"task_id": "task", "mode": "loom"}), "loom-image")
