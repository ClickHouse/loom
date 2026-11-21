"""Public bundles expose measured scalar evidence without private trial assets."""

import hashlib
import unittest

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor import public_export
from examples.software_factory.tests.harbor import test_harbor_reporting as fixtures
from examples.software_factory.tests.harbor.test_harbor_reporting import (
    hashes,
    read,
    write,
)


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.HarborReporting()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.add(33)
        self.protocol = self.fixture.protocol
        self.protocol.update(
            model_id="fixture-model", schedule_sha256=digest(self.protocol["schedule"])
        )
        self.protocol["public_reproduction"] = {
            "schema_version": 1,
            "suite": {
                "id": "sample",
                "repository_url": "https://github.com/example/public",
                "languages": ["python"],
                "source_roots": ["src/example"],
            },
            "model": {
                "id": "fixture-model",
                "provider": "anthropic-messages",
                "api_version": "2023-06-01",
            },
            "dataset": {
                "repository": "example/public",
                "revision": "a" * 40,
                "file": "data/test.parquet",
                "sha256": "b" * 64,
            },
            "tasks": [
                {"task_id": t, "base_commit": "c" * 40, "record_sha256": "d" * 64}
                for t in self.fixture.tasks
            ],
            "dependencies": {
                "base_images": ["python@sha256:" + "e" * 64],
                "tasks": [],
                "package_artifacts": [],
            },
            "indexers": {"tools": [{"name": "scip-python", "version": "0.6.6"}]},
            "pricing": {
                "source": "https://platform.claude.com/docs/en/about-claude/pricing",
                "input_usd_per_million": 0.1,
                "output_usd_per_million": 0.5,
            },
        }
        self.protocol["credentials_file"] = "/Users/PRIVATE_PATH/token.env"
        self.protocol["headers"] = {"Authorization": "CREDENTIAL_SENTINEL"}
        self.protocol["server_source_identity"] = {
            "files": {"/Users/PRIVATE_PATH/secrets.env": "a" * 64}
        }
        self.protocol["public_reproduction"]["credentials"] = "CREDENTIAL_SENTINEL"
        for order, raw in enumerate(self.fixture.raw):
            raw["agent_result"]["metadata"] = {
                "factory": {
                    "task_id": self.protocol["schedule"][order]["task_id"],
                    "generation_cost_usd": 0.0 if order == 0 else raw["agent_result"]["cost_usd"],
                    "agent_time_s": 1.0,
                    "agent_test_runs": 0,
                    "loom_tool_calls": 0,
                    "memory_rows_written": 0,
                    "loom_logical_requests": 0,
                    "loom_physical_requests": 0,
                    "loom_transport_dispatches": 0,
                    "model_selected_loom_requests": 0,
                    "provider_logical_requests": 1,
                    "provider_failed_requests": 0,
                    "provider_usage": [
                        {
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                        }
                    ],
                    "transport_headers": {"Authorization": "CREDENTIAL_SENTINEL"},
                    "error": "/Users/PRIVATE_PATH/internal-repo",
                }
            }
            folder = self.fixture.source / raw["trial_name"]
            raw["agent_result"]["cost_usd"] = raw["agent_result"]["metadata"]["factory"][
                "generation_cost_usd"
            ]
            write(folder / "result.json", raw)
            write(folder / "factory-measurement.json", {"end_to_end_s": 40.0})
        self.output = self.fixture.root.parent / "public"

    def test_allowlist_exports_all_failures_controls_and_no_private_bytes(self):
        before = hashes(self.fixture.source)
        result = public_export.export_public(self.protocol, self.output, require_complete=True)
        self.assertTrue(result["complete"])
        rows = read(self.output / "attempts.json")
        self.assertEqual(len(rows), 33)
        self.assertEqual(sum(row["arm"] in {"baseline-a", "baseline-b"} for row in rows), 6)
        self.assertEqual(sum(row["resolved"] is True for row in rows), 18)
        for path in self.output.rglob("*"):
            if path.is_file():
                data = path.read_text()
                for banned in (
                    "CREDENTIAL_SENTINEL",
                    "PRIVATE_PATH",
                    "Authorization",
                    "internal-repo",
                    str(self.fixture.root),
                ):
                    self.assertNotIn(banned, data, str(path))
        self.assertEqual(before, hashes(self.fixture.source))
        for relative, expected in result["files"].items():
            path = self.output / relative
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            self.assertEqual(path.stat().st_nlink, 1)
        self.assertTrue((self.output / "charts/reports/charts.html").is_file())

    def test_native_reward_wins_over_an_edited_private_summary(self):
        write(
            self.fixture.root / "live-attempts.json",
            [
                {"order": 0, "result": {"resolved": True, "generation_cost_usd": 999999}},
            ],
        )
        public_export.export_public(self.protocol, self.output)
        rows = read(self.output / "attempts.json")
        self.assertEqual(
            rows[0]["resolved"], self.fixture.raw[0]["verifier_result"]["rewards"]["reward"] == 1
        )
        self.assertEqual(rows[0]["generation_cost_usd"], 0)

    def test_unknown_measurements_stay_unknown_and_known_zero_stays_zero(self):
        path = self.fixture.source / self.fixture.raw[1]["trial_name"] / "result.json"
        raw = read(path)
        factory = raw["agent_result"]["metadata"]["factory"]
        factory["generation_cost_usd"] = None
        factory["provider_failed_requests"] = 1
        factory["loom_logical_requests"] = factory["loom_physical_requests"] = 3
        write(path, raw)
        public_export.export_public(self.protocol, self.output)
        rows = read(self.output / "attempts.json")
        self.assertEqual(rows[0]["total_provider_cost_usd"], 0)
        self.assertIsNone(rows[1]["generation_cost_usd"])
        self.assertIsNone(rows[1]["total_provider_cost_usd"])
        self.assertIsNone(rows[1]["model_total_tokens"])
        self.assertIsNone(rows[1]["judge_fixed"])
        self.assertIsNone(rows[1]["full_suite_regressions"])

    def test_partial_bundle_has_pending_slots_and_cannot_claim_final(self):
        import shutil

        shutil.rmtree(self.fixture.source / self.fixture.raw[-1]["trial_name"])
        with self.assertRaises(ValueError):
            public_export.export_public(self.protocol, self.output, require_complete=True)
        self.assertFalse(self.output.exists())
        result = public_export.export_public(self.protocol, self.output)
        self.assertFalse(result["complete"])
        self.assertEqual(read(self.output / "attempts.json")[-1]["state"], "pending")

    def test_unowned_output_and_symlinked_native_results_are_refused(self):
        self.output.mkdir()
        (self.output / "personal.txt").write_text("keep")
        with self.assertRaises(PermissionError):
            public_export.export_public(self.protocol, self.output)
        self.assertEqual((self.output / "personal.txt").read_text(), "keep")

    def test_owned_output_can_refresh_without_touching_native_files(self):
        public_export.export_public(self.protocol, self.output)
        first = hashes(self.output)
        public_export.export_public(self.protocol, self.output)
        self.assertEqual(first, hashes(self.output))

    def test_independent_suite_judge_and_billing_proofs_are_not_replaced_by_zero(self):
        import copy

        folder = self.fixture.source / self.fixture.raw[0]["trial_name"]
        raw = read(folder / "result.json")
        factory = raw["agent_result"]["metadata"]["factory"]
        for key in ("loom_logical_requests", "loom_physical_requests", "loom_transport_dispatches"):
            factory[key] = 3
        write(folder / "result.json", raw)
        base_folder = self.fixture.root / "jobs/base/base-trial"
        base = copy.deepcopy(raw)
        base.update(trial_name="base-trial", trial_uri=base_folder.as_uri())
        base["config"]["trial_name"] = "base-trial"
        write(base_folder / "result.json", base)
        write(
            base_folder / "verifier/full-suite.json",
            {
                "valid": True,
                "status": "completed",
                "tests": {"a": "PASSED", "b": "PASSED"},
                "exit_code": 0,
            },
        )
        write(
            folder / "verifier/full-suite.json",
            {
                "valid": True,
                "status": "completed",
                "tests": {"a": "FAILED", "b": "PASSED"},
                "exit_code": 1,
            },
        )
        write(folder / "verifier/grade.json", {"state": "a" * 64})
        write(
            self.fixture.root / "judgements/verdicts.json",
            {
                "protocol_sha256": digest(self.protocol),
                "attempts": {
                    "0": {
                        "candidate_state": "a" * 64,
                        "judge_fixed": False,
                        "source": "/Users/PRIVATE_PATH/judge.json",
                    }
                },
            },
        )
        write(
            self.fixture.root / "provider-costs.json",
            {
                "protocol_sha256": digest(self.protocol),
                "attempts": {
                    "0": {
                        "proof_status": "known",
                        "physical_requests_dispatched": 3,
                        "physical_requests_matched": 3,
                        "loom_provider_cost_usd": 0.25,
                        "input_tokens": 100,
                        "output_tokens": 10,
                    }
                },
            },
        )
        public_export.export_public(self.protocol, self.output)
        row = read(self.output / "attempts.json")[0]
        self.assertEqual(row["full_suite_regressions"], 1)
        self.assertIs(row["judge_fixed"], False)
        self.assertEqual(row["total_provider_cost_usd"], 0.25)
        self.assertEqual(row["model_total_tokens"], 12)

    def test_incomplete_billing_proof_and_state_mismatch_stay_unknown(self):
        folder = self.fixture.source / self.fixture.raw[0]["trial_name"]
        raw = read(folder / "result.json")
        raw["agent_result"]["metadata"]["factory"]["loom_transport_dispatches"] = 3
        write(folder / "result.json", raw)
        write(folder / "verifier/grade.json", {"state": "a" * 64})
        write(
            self.fixture.root / "judgements/verdicts.json",
            {
                "protocol_sha256": digest(self.protocol),
                "attempts": {
                    "0": {"candidate_state": "b" * 64, "judge_fixed": True},
                },
            },
        )
        write(
            self.fixture.root / "provider-costs.json",
            {
                "protocol_sha256": digest(self.protocol),
                "attempts": {
                    "0": {"proof_status": "unknown", "loom_provider_cost_usd": 0},
                },
            },
        )
        public_export.export_public(self.protocol, self.output)
        row = read(self.output / "attempts.json")[0]
        self.assertIsNone(row["judge_fixed"])
        self.assertIsNone(row["loom_provider_cost_usd"])

    def test_invalid_native_verification_remains_a_scheduled_failure(self):
        from harbor.models.trial.result import TrialResult

        path = self.fixture.source / self.fixture.raw[0]["trial_name"] / "result.json"
        raw = read(path)
        raw["verifier_result"] = None
        write(path, TrialResult.model_validate(raw).model_dump(mode="json"))
        public_export.export_public(self.protocol, self.output)
        rows = read(self.output / "attempts.json")
        self.assertEqual(len(rows), 33)
        self.assertFalse(rows[0]["resolved"])
        self.assertEqual(rows[0]["state"], "invalid_verifier")

    def test_native_result_symlinks_are_refused(self):
        path = self.fixture.source / self.fixture.raw[0]["trial_name"] / "result.json"
        moved = path.with_name("original.json")
        path.rename(moved)
        path.symlink_to(moved)
        with self.assertRaises(PermissionError):
            public_export.export_public(self.protocol, self.output)

    def test_overlapping_output_is_refused_before_native_tree_is_written(self):
        before = hashes(self.fixture.source)
        with self.assertRaises(PermissionError):
            public_export.export_public(self.protocol, self.fixture.source / "public")
        self.assertEqual(before, hashes(self.fixture.source))

    def test_export_never_calls_provider_or_ledger(self):
        from unittest.mock import patch

        with patch("httpx.Client.request", side_effect=AssertionError("network forbidden")):
            public_export.export_public(self.protocol, self.output)

    def test_conflicting_native_costs_remain_unknown(self):
        path = self.fixture.source / self.fixture.raw[0]["trial_name"] / "result.json"
        raw = read(path)
        raw["agent_result"]["metadata"]["factory"]["generation_cost_usd"] = 9000
        write(path, raw)
        public_export.export_public(self.protocol, self.output)
        self.assertIsNone(read(self.output / "attempts.json")[0]["generation_cost_usd"])

    def test_bad_schedule_or_public_task_identity_is_refused(self):
        self.protocol["public_reproduction"]["tasks"][0]["base_commit"] = "e" * 40
        with self.assertRaises(ValueError):
            public_export.export_public(self.protocol, self.output)


if __name__ == "__main__":
    unittest.main()
