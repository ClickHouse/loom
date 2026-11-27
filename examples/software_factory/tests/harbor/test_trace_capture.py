"""Private stage evidence stays scoped and retains missing-data disclosures."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.software_factory.harbor.runtime import write
from examples.software_factory.harbor.trace_capture import capture, capture_or_report


class TraceCapture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "diagnostics").mkdir()
        self.protocol = {
            "output_root": str(self.root),
            "server_container": "private-server",
            "attempt_scopes": {"0": {"org": "org", "code_namespace": "code"}},
        }
        self.order = 0
        self.native = self.root / "diagnostics/attempt-00.json"
        self.summary = {
            "source_sha256": "a" * 64,
            "calls": [{"trace_run_id": "run-1"}],
        }
        write(self.native, self.summary)
        self.row = {
            "org": "org",
            "namespace": "code",
            "trace_run_id": "run-1",
            "op": "code.grep",
            "stage_ms": {"chunks": 12, "bodies": 7},
        }
        self.request = {
            "org": "org",
            "namespace": "code",
            "trace_run_id": "run-1",
            "request_id": "request-1",
            "operation": "code.grep",
            "latency_ms": 34,
            "stage_ms": "chunks=12,lessons=10,bodies=7",
        }

    def observed(self, rows, requests=None):
        if requests is None:
            requests = [self.request] if rows else []
        return patch(
            "examples.software_factory.harbor.trace_capture.subprocess.run",
            return_value=SimpleNamespace(
                stdout=json.dumps({"rows": rows, "requests": requests, "ledger_error": None})
            ),
        )

    def test_complete_capture_retains_stage_evidence_and_needs_no_live_server_again(self):
        with self.observed([self.row]) as query:
            receipt = capture(self.protocol, self.order)
            self.assertEqual(capture(self.protocol, self.order), receipt)
        self.assertEqual(query.call_count, 1)
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["rows"], [self.row])
        self.assertEqual(receipt["missing_trace_run_ids"], [])
        self.assertEqual(receipt["request_ledger"], [self.request])
        self.assertEqual(receipt["request_ledger_missing_trace_run_ids"], [])

    def test_missing_async_rows_stay_partial_and_can_be_captured_later(self):
        with self.observed([]):
            first = capture(self.protocol, self.order)
        self.assertFalse(first["complete"])
        self.assertEqual(first["missing_trace_run_ids"], ["run-1"])
        with self.observed([self.row]):
            self.assertTrue(capture(self.protocol, self.order)["complete"])

    def test_complete_search_trace_is_retried_for_delayed_ledger_evidence(self):
        with self.observed([self.row], []):
            first = capture(self.protocol, self.order)
        self.assertEqual(first["request_ledger_missing_trace_run_ids"], ["run-1"])
        with patch(
            "examples.software_factory.harbor.trace_capture.subprocess.run",
            side_effect=OSError("private error payload"),
        ):
            delayed = capture(self.protocol, self.order)
        self.assertTrue(delayed["complete"])
        self.assertEqual(delayed["missing_trace_run_ids"], [])
        self.assertEqual(delayed["request_ledger_missing_trace_run_ids"], ["run-1"])
        with self.observed([], [self.request]):
            second = capture(self.protocol, self.order)
        self.assertEqual(second["rows"], [self.row])
        self.assertEqual(second["request_ledger"], [self.request])
        self.assertEqual(second["request_ledger_missing_trace_run_ids"], [])

    def test_other_scope_or_unrequested_ids_never_enter_the_saved_rows(self):
        for changed in ({"org": "other"}, {"namespace": "other"}, {"trace_run_id": "other"}):
            with self.subTest(changed=changed), self.observed([{**self.row, **changed}]):
                receipt = capture(self.protocol, self.order)
            self.assertEqual(receipt["state"], "capture_error")
            self.assertEqual(receipt["rows"], [])
            self.assertEqual(receipt["error_type"], "PermissionError")

    def test_capture_error_preserves_prior_rows_without_error_payloads(self):
        write(
            self.native,
            {**self.summary, "calls": [{"trace_run_id": name} for name in ("run-1", "run-2")]},
        )
        with self.observed([self.row]):
            capture(self.protocol, self.order)
        with patch(
            "examples.software_factory.harbor.trace_capture.subprocess.run",
            side_effect=OSError("private error payload"),
        ):
            receipt = capture(self.protocol, self.order)
        self.assertEqual(receipt["rows"], [self.row])
        self.assertEqual(receipt["request_ledger"], [self.request])
        self.assertFalse(receipt["complete"])
        self.assertEqual(receipt["missing_trace_run_ids"], ["run-2"])
        self.assertNotIn("private error payload", json.dumps(receipt))

    def test_missing_native_evidence_remains_unknown_then_can_be_supplied(self):
        self.native.unlink()
        with self.observed([self.row]) as query:
            receipt = capture(self.protocol, self.order)
            query.assert_not_called()
            self.assertFalse(receipt["complete"])
            self.assertEqual(receipt["state"], "unavailable")
            write(self.native, self.summary)
            self.assertTrue(capture(self.protocol, self.order)["complete"])

    def test_changed_recorded_source_does_not_overwrite_prior_evidence(self):
        with self.observed([self.row]):
            capture(self.protocol, self.order)
        write(self.native, {**self.summary, "source_sha256": "b" * 64})
        with self.assertRaises(PermissionError):
            capture(self.protocol, self.order)

    def test_no_returned_trace_ids_is_distinct_from_missing_rows(self):
        write(self.native, {**self.summary, "calls": []})
        with self.observed([]) as query:
            receipt = capture(self.protocol, self.order)
        query.assert_not_called()
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["state"], "no_trace_ids_returned")

    def test_optional_capture_reports_disk_failure_without_raising(self):
        with (
            patch(
                "examples.software_factory.harbor.trace_capture.write",
                side_effect=OSError("private disk detail"),
            ),
            self.assertLogs(
                "examples.software_factory.harbor.trace_capture", level="WARNING"
            ) as logs,
        ):
            capture_or_report(self.protocol, self.order)
        self.assertIn("attempt 0 (OSError)", logs.output[0])
        self.assertNotIn("private disk detail", logs.output[0])
