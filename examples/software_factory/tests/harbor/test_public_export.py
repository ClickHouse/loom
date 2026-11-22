"""Public bundles derive measurements from original native artifacts."""

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.public_export import export_public
from examples.software_factory.harbor.runtime import write


class NativePublicExport(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        trial = self.root / "jobs/live/trial"
        (trial / "agent").mkdir(parents=True)
        self.trial = trial
        self.instruction = b"Original issue\r\nPreserve source bytes.\r\n"
        instruction = self.root / "tasks/slot-00/instruction.md"
        instruction.parent.mkdir(parents=True)
        instruction.write_bytes(self.instruction)
        (trial / "agent/claude-code.txt").write_text(
            json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}) + "\n"
        )
        slot = {
            "order": 0,
            "task_id": "task",
            "mode": "baseline",
            "arm": "baseline",
            "index_tier": None,
            "memory_enabled": False,
            "repeat": 0,
        }
        marker = self.root / "input.json"
        marker.write_text("{}")
        self.protocol = {
            "output_root": str(self.root),
            "controller": "harbor",
            "harbor_version": "0.27.0",
            "model_id": "claude-sonnet-5-5",
            "index_tier": "per-arm",
            "schedule": [slot],
            "schedule_sha256": digest([slot]),
            "solver": {"name": "claude-code", "version": "pinned"},
            "files": {str(marker): hashlib.sha256(marker.read_bytes()).hexdigest()},
            "suite": {"adapter": "examples.software_factory.evals.flask.profile"},
            "public_reproduction": {
                "suite": {
                    "id": "flask",
                    "repository_url": "https://github.com/pallets/flask",
                    "languages": ["python"],
                    "source_roots": ["src/flask"],
                },
                "model": {
                    "id": "claude-sonnet-5-5",
                    "provider": "anthropic",
                    "api_version": "2023-06-01",
                },
                "tasks": [],
            },
        }
        write(
            trial / "result.json",
            {
                "agent_info": {"name": "claude-code"},
                "config": {"task": {"path": str(self.root / "tasks/slot-00")}},
                "reward_valid": False,
                "verifier_result": {"rewards": {"reward": 1}},
                "started_at": "2000-01-01T00:00:00Z",
                "finished_at": "2000-01-01T00:00:10Z",
            },
        )
        write(
            trial / "agent/trajectory.json",
            {
                "agent": {"name": "claude-code", "version": "pinned"},
                "steps": [],
                "final_metrics": {
                    "total_prompt_tokens": 10,
                    "total_completion_tokens": 2,
                    "total_cost_usd": 0.02,
                },
            },
        )
        write(
            self.root / "live-attempts.json",
            [
                {
                    "order": 0,
                    "result": {
                        "harbor_trial": str(trial),
                        "generation_cost_usd": 0,
                        "resolved": True,
                        "secret": "PRIVATE_SENTINEL",
                    },
                }
            ],
        )

    def test_native_reward_and_spend_override_edited_summary(self):
        result = export_public(self.protocol, self.root / "public", require_complete=True)
        self.assertTrue(result["complete"])
        path = self.root / "public/results.json"
        public = json.loads(path.read_text())
        row = public["attempts"][0]
        self.assertFalse(row["resolved"])
        self.assertEqual(row["generation_cost_usd"], 0.02)
        self.assertEqual(row["model_total_tokens"], 12)
        self.assertEqual(row["tool_total_requests"], 0)
        self.assertNotIn("PRIVATE_SENTINEL", path.read_text())
        self.assertNotIn(str(self.root), path.read_text())
        self.assertIn("0/result.json", public["native_artifact_sha256"])
        self.assertEqual(
            public["native_artifact_sha256"]["0/task/instruction.md"],
            hashlib.sha256(self.instruction).hexdigest(),
        )
        self.assertEqual(
            public["native_artifact_sha256"]["0/agent/claude-code.txt"],
            hashlib.sha256((self.trial / "agent/claude-code.txt").read_bytes()).hexdigest(),
        )

    def test_symlink_or_foreign_native_result_is_refused(self):
        native = self.trial / "result.json"
        original = native.read_bytes()
        native.unlink()
        other = self.root / "other.json"
        other.write_bytes(original)
        native.symlink_to(other)
        with self.assertRaises(PermissionError):
            export_public(self.protocol, self.root / "public")

    def test_changed_frozen_input_is_refused_before_export(self):
        Path(next(iter(self.protocol["files"]))).write_text("changed")
        with self.assertRaises(PermissionError):
            export_public(self.protocol, self.root / "public")
