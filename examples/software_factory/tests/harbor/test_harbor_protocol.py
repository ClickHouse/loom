import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor import study as harbor_study
from examples.software_factory.harbor.runtime import verify


class HarborProtocol(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        frozen = self.root / "source.py"
        frozen.write_text("value = 1\n")
        identity = {str(frozen): hashlib.sha256(frozen.read_bytes()).hexdigest()}
        controller = self.root / "controller.py"
        controller.write_text("controller = True\n")
        controller_files = {str(controller): hashlib.sha256(controller.read_bytes()).hexdigest()}
        self.plan = {
            "controller": "harbor",
            "harbor_version": "0.27.0",
            "schedule": [],
            "schedule_sha256": digest([]),
            "files": {**identity, **controller_files},
            "public_inputs": identity,
            "server_source_identity": {"files": identity},
            "controller_source_identity": {"files": controller_files},
            "model_id": "claude-haiku-5-5",
            "index_tier": "per-arm",
            "tasks": {name: {} for name in ("a", "b", "c")},
            "output_root": str(self.root),
        }

    def test_native_protocol_requires_no_predecessor_control_receipts(self):
        verify(self.plan)
        changed = {**self.plan, "controller": "other"}
        with self.assertRaises(PermissionError):
            verify(changed)

    def test_each_priced_model_can_be_frozen_but_unknown_models_refuse(self):
        verify({**self.plan, "model_id": "claude-sonnet-5-5"})
        with self.assertRaises(PermissionError):
            verify({**self.plan, "model_id": "unpriced-model"})

    def test_input_freeze_verifies_public_provenance_and_every_server_source(self):
        paths = harbor_study.verified_input_files(self.plan)
        self.assertEqual(set(paths), {Path(p) for p in self.plan["files"]})
        for field in ("public_inputs", "server_source_identity", "controller_source_identity"):
            changed = json.loads(json.dumps(self.plan))
            hashes = changed[field] if field == "public_inputs" else changed[field]["files"]
            hashes[next(iter(hashes))] = "0" * 64
            with self.assertRaises(PermissionError):
                harbor_study.verified_input_files(changed)

    def controls(self):
        for kind in ("base", "reference"):
            rows = [
                {
                    "result": {
                        "task_id": name,
                        "harbor_reward_valid": True,
                        "resolved": kind == "reference",
                        "provider_physical_requests": 0,
                        "full_suite": {"valid": True},
                        "full_suite_regressions": 0,
                        "protocol_sha256": digest(self.plan),
                    }
                }
                for name in self.plan["tasks"]
            ]
            path = self.root / (kind + "-qualification.json")
            path.write_text(
                json.dumps(
                    {"qualified": True, "protocol_sha256": digest(self.plan), "attempts": rows}
                )
            )

    def test_live_requires_native_controls_for_this_exact_protocol_and_all_tasks(self):
        self.controls()
        harbor_study.verify_qualifications(self.plan)
        self.plan["model_id"] = "changed"
        with self.assertRaises(PermissionError):
            harbor_study.verify_qualifications(self.plan)

    def test_qualification_boolean_cannot_hide_duplicate_tasks_or_paid_requests(self):
        self.controls()
        path = self.root / "reference-qualification.json"
        original = json.loads(path.read_text())
        for mutate in (
            lambda row: row.update(task_id="a"),
            lambda row: row.update(provider_physical_requests=1),
            lambda row: row.update(resolved=False),
            lambda row: row.update(harbor_reward_valid=False),
        ):
            changed = json.loads(json.dumps(original))
            mutate(changed["attempts"][1]["result"])
            path.write_text(json.dumps(changed))
            with self.assertRaises(PermissionError):
                harbor_study.verify_qualifications(self.plan)
