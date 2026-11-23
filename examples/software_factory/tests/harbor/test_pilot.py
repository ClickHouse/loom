"""Pilot selection preserves frozen issue, history and grading inputs."""

import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.harbor.pilot import select
from examples.software_factory.harbor.source import sha


class PilotSelection(unittest.TestCase):
    def test_selection_copies_public_inputs_without_rewriting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "historical.json"
            controls = root / "reference.json"
            public = [{"task_id": task, "problem": "Frozen issue " + task} for task in ("a", "b")]
            reference = [
                {"instance_id": task, "patch": "Frozen patch " + task} for task in ("a", "b")
            ]
            manifest.write_text(json.dumps({"tasks": public}))
            controls.write_text(json.dumps(reference))
            hashes = {str(path): sha(path) for path in (manifest, controls)}
            prepared = {
                "tasks": {task: {"base_commit": task * 40} for task in ("a", "b")},
                "memory_histories": {task: {"fixture_sha256": task * 64} for task in ("a", "b")},
                "schedule": schedule(["a", "b"], seed=741),
                "historical_manifest": str(manifest),
                "reference_controls_file": str(controls),
                "public_inputs": hashes,
            }
            selected = select(prepared, "b", root / "pilot")
            self.assertEqual(set(selected["tasks"]), {"b"})
            self.assertEqual(selected["tasks"]["b"], prepared["tasks"]["b"])
            self.assertEqual(selected["memory_histories"], {"b": prepared["memory_histories"]["b"]})
            self.assertEqual(
                json.loads(Path(selected["historical_manifest"]).read_text()),
                {"tasks": [public[1]]},
            )
            self.assertEqual(
                json.loads(Path(selected["reference_controls_file"]).read_text()), [reference[1]]
            )
            self.assertEqual(hashes, {str(path): sha(path) for path in (manifest, controls)})
            self.assertEqual(len(prepared["schedule"]), 34)
            self.assertEqual(len(selected["schedule"]), 5)
            for path, digest in selected["public_inputs"].items():
                self.assertEqual(sha(Path(path)), digest)

    def test_unknown_task_refuses_before_creating_selection(self):
        prepared = {"tasks": {"a": {}}, "schedule": schedule(["a"], seed=741)}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pilot"
            with self.assertRaisesRegex(ValueError, "frozen evaluation"):
                select(prepared, "b", output)
            self.assertFalse(output.exists())
