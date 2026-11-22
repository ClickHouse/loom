"""Native usage attribution retains errors and unknowns."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from examples.software_factory.harbor.billing import audit_provider_costs


class NativeBilling(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.protocol = {
            "output_root": str(self.root),
            "schedule": [{"mode": "baseline"}, {"mode": "natural"}],
            "attempt_scopes": {
                "0": {},
                "1": {
                    "org": "org",
                    "token_subject": "sa:attempt-1",
                    "code_namespace": "code",
                    "memory_namespace": "memory",
                },
            },
        }
        self.entries = [{"order": n, "result": {"loom_tool_calls": n}} for n in range(2)]
        self.row = {
            "org": "org",
            "namespace": "code",
            "subject": "sa:attempt-1",
            "request_id": "request",
            "operation": "code.search",
            "success": False,
            "error_code": "FAILED",
            "upstream_cost_usd": "0.03",
            "input_tokens": 11,
            "output_tokens": 2,
            "embedding_tokens": 4,
            "response_tokens": 8,
        }

    def test_failed_operation_cost_is_counted_and_saved_replay_is_offline(self):
        loader = Mock(return_value={"events": [self.row], "memory_rows_written": 2})
        receipt = audit_provider_costs(self.protocol, self.entries, ledger_loader=loader)
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(receipt["attempts"]["0"]["loom_provider_cost_usd"], 0)
        proof = receipt["attempts"]["1"]
        self.assertEqual(proof["proof_status"], "known")
        self.assertEqual(proof["loom_provider_cost_usd"], 0.03)
        self.assertEqual(proof["loom_provider_input_tokens"], 11)
        self.assertEqual(proof["loom_response_tokens"], 8)
        self.assertEqual(proof["memory_rows_written"], 2)
        loader.reset_mock()
        self.assertEqual(
            audit_provider_costs(self.protocol, self.entries, ledger_loader=loader), receipt
        )
        loader.assert_not_called()

    def test_foreign_subject_missing_rows_and_duplicate_ids_are_unknown(self):
        for rows in ([{**self.row, "subject": "sa:other"}], [], [self.row, self.row]):
            with tempfile.TemporaryDirectory() as directory:
                protocol = {**self.protocol, "output_root": directory}
                receipt = audit_provider_costs(
                    protocol,
                    self.entries,
                    ledger_loader=lambda *_, rows=rows: {"events": rows, "memory_rows_written": 0},
                )
                proof = receipt["attempts"]["1"]
                self.assertEqual(proof["proof_status"], "unknown")
                self.assertIsNone(proof["loom_provider_cost_usd"])

    def test_saved_ledger_change_is_refused(self):
        audit_provider_costs(
            self.protocol,
            self.entries,
            ledger_loader=lambda *_: {"events": [self.row], "memory_rows_written": 0},
        )
        path = self.root / "provider-ledger.json"
        saved = json.loads(path.read_text())
        saved["attempts"]["1"]["events"][0]["upstream_cost_usd"] = "0"
        path.write_text(json.dumps(saved))
        with self.assertRaises(PermissionError):
            audit_provider_costs(self.protocol, self.entries)
