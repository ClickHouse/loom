import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.runtime import ObservedProvider, verify


class LiveControllerContracts(unittest.TestCase):
    def test_large_usage_is_observed_without_a_spend_or_input_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"

            def dispatch(*args):
                self.assertEqual(json.loads(path.read_text().splitlines()[0])["phase"], "intent")
                return {
                    "usage": {
                        "input_tokens": 30000,
                        "output_tokens": 400,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "blocks": [],
                }

            provider = ObservedProvider(
                Mock(label="fixture", model_id="claude-haiku-5-5", step=Mock(side_effect=dispatch)),
                path,
            )
            result = provider.step([], [], 2048)
            self.assertAlmostEqual(result["generation_cost_usd"], 0.0032)
            self.assertEqual(result["usage"]["input_tokens"], 30000)
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_frozen_source_change_is_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_text("changed")
            protocol = {
                "controller": "harbor",
                "harbor_version": "0.27.0",
                "schedule": [{"order": 0}],
                "schedule_sha256": digest([{"order": 0}]),
                "files": {str(path): "0" * 64},
            }
            with self.assertRaisesRegex(PermissionError, "frozen runtime"):
                verify(protocol)

    def test_failed_dispatch_is_retained_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            transport = Mock(
                label="fixture", step=Mock(side_effect=RuntimeError("offline failure"))
            )
            with self.assertRaises(RuntimeError):
                ObservedProvider(transport, path).step([], [], 2048)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["phase"] for row in rows], ["intent", "failed"])
            self.assertEqual(transport.step.call_count, 1)
