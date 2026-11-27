import json
import tempfile
import unittest
from pathlib import Path

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.study import enrich_evaluations


class EvaluationEnrichment(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.protocol = {
            "output_root": str(self.root),
            "suite": {"adapter": "examples.software_factory.evals.flask.profile"},
        }
        self.result = {
            "task_id": "issue",
            "generation_cost_usd": 0.02,
            "provider_logical_requests": 1,
            "provider_failed_requests": 0,
            "provider_usage": [
                {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            ],
            "full_suite": {"valid": True, "tests": {"old": "FAILED", "kept": "PASSED"}},
            "hidden_grade": {"state": "candidate"},
        }
        self.entries = [{"order": 0, "result": self.result}]

    def baseline(self):
        (self.root / "base-qualification.json").write_text(
            json.dumps(
                {
                    "protocol_sha256": digest(self.protocol),
                    "attempts": [
                        {
                            "result": {
                                "task_id": "issue",
                                "full_suite": {
                                    "valid": True,
                                    "tests": {"old": "FAILED", "kept": "PASSED"},
                                },
                            }
                        }
                    ],
                }
            )
        )

    def test_measured_tokens_and_regressions_do_not_treat_old_failures_as_new(self):
        self.baseline()
        enrich_evaluations(self.protocol, self.entries, "live")
        self.assertEqual(self.result["model_total_tokens"], 110)
        self.assertEqual(self.result["full_suite_regressions"], 0)
        self.result["full_suite"]["tests"]["kept"] = "FAILED"
        enrich_evaluations(self.protocol, self.entries, "live")
        self.assertEqual(self.result["full_suite_regressions"], 1)
        self.assertEqual(self.result["full_suite_regression_tests"], ["kept"])

    def test_missing_suite_or_judge_is_unknown_and_judge_must_match_candidate(self):
        enrich_evaluations(self.protocol, self.entries, "live")
        self.assertIsNone(self.result["full_suite_regressions"])
        self.assertIsNone(self.result["judge_fixed"])
        folder = self.root / "judgements"
        folder.mkdir()
        path = folder / "verdicts.json"
        for state, expected in (("different", None), ("candidate", True)):
            path.write_text(
                json.dumps(
                    {
                        "protocol_sha256": digest(self.protocol),
                        "attempts": {"0": {"candidate_state": state, "judge_fixed": True}},
                    }
                )
            )
            enrich_evaluations(self.protocol, self.entries, "live")
            self.assertIs(self.result["judge_fixed"], expected)

    def test_provider_cost_requires_a_matching_backend_receipt(self):
        enrich_evaluations(self.protocol, self.entries, "live")
        self.assertIsNone(self.result["total_provider_cost_usd"])
        path = self.root / "provider-costs.json"
        receipt = {
            "protocol_sha256": digest(self.protocol),
            "attempts": {"0": {"loom_provider_cost_usd": 0, "proof_status": "known"}},
        }
        path.write_text(json.dumps(receipt))
        enrich_evaluations(self.protocol, self.entries, "live")
        self.assertEqual(self.result["total_provider_cost_usd"], 0.02)
        receipt["protocol_sha256"] = "wrong"
        path.write_text(json.dumps(receipt))
        with self.assertRaises(PermissionError):
            enrich_evaluations(self.protocol, self.entries, "live")
