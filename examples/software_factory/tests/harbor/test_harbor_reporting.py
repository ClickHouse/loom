"""Native comparison views preserve measured trials and represent pending cells."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult, JobStats
from harbor.models.trial.result import TrialResult

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.reporting import PRIMARY_VIEWS, generate_reports


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def read(path):
    return json.loads(path.read_text())


def hashes(folder):
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in folder.rglob("*")
        if p.is_file()
    }


class HarborReporting(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "study"
        self.source = self.root / "jobs/live"
        self.start = datetime(2000, 1, 1, tzinfo=UTC)
        self.source_id = uuid5(NAMESPACE_URL, "fixture-study")
        self.entries, self.raw, self.locks = [], [], []
        self.tasks = ["scenario-a", "scenario-b", "scenario-c"]
        schedule = []
        for task in self.tasks:
            for repeat in range(3):
                arms = list(PRIMARY_VIEWS)
                for arm in arms[repeat:] + arms[:repeat]:
                    schedule.append(
                        {
                            "order": len(schedule),
                            "task_id": task,
                            "repeat": repeat,
                            "arm": arm,
                            "mode": "baseline" if arm == "baseline" else "natural",
                            "index_tier": {
                                "loom-treesitter": "treesitter",
                                "loom-scip": "scip",
                            }.get(arm),
                        }
                    )
        for task in self.tasks:
            for arm in ["baseline-a", "baseline-b"]:
                schedule.append(
                    {
                        "order": len(schedule),
                        "task_id": task,
                        "repeat": "aa",
                        "arm": arm,
                        "mode": "baseline",
                        "index_tier": None,
                    }
                )
        self.protocol = {
            "suite": {"adapter": "examples.software_factory.evals.flask.profile"},
            "output_root": str(self.root),
            "reporting": {"publish_charts": False},
            "schedule": schedule,
            "historical_manifest": str(self.root / "historical.json"),
            "harbor_specs": {
                task: {"source_sha256": "a" * 64, "grader_sha256": "b" * 64} for task in self.tasks
            },
            "cost_basis": {"type": "fixture generation estimate", "infrastructure": "unknown"},
        }
        write(
            self.root / "historical.json",
            {"tasks": [{"task_id": task, "base_commit": "c" * 40} for task in self.tasks]},
        )
        self.config = JobConfig.model_validate(
            {
                "job_name": "live",
                "jobs_dir": str(self.root / "jobs"),
                "n_concurrent_trials": 1,
                "agents": [{"import_path": "fixture:Agent", "model_name": "fixture-model"}],
                "tasks": [
                    {"path": str(self.root / "tasks" / f"slot-{s['order']:02d}")} for s in schedule
                ],
            }
        ).model_dump(mode="json")
        write(self.source / "config.json", self.config)
        self.sync_job()

    def sync_job(self):
        results = [TrialResult.model_validate(r) for r in self.raw]
        aggregate = JobResult(
            id=self.source_id,
            started_at=self.start,
            updated_at=self.start + timedelta(seconds=len(results) * 50),
            finished_at=self.start + timedelta(seconds=2000) if len(results) == 33 else None,
            n_total_trials=33,
            stats=JobStats.from_trial_results(results, n_total_trials=33),
        )
        write(self.source / "result.json", aggregate.model_dump(mode="json"))
        write(
            self.source / "lock.json",
            {
                "schema_version": 3,
                "created_at": self.start.isoformat(),
                "harbor": {"version": "0.27.0"},
                "n_concurrent_trials": 1,
                "retry": {"max_retries": 0},
                "trials": self.locks,
            },
        )

    def add(self, count=1, *, unknown_cost=False):
        totals = {
            "baseline": 0.0876721,
            "loom-treesitter": 0.0875002,
            "loom-scip": 0.0856215,
            "baseline-a": 0.0278679,
            "baseline-b": 0.0251254,
        }
        successes = {
            "baseline": 3,
            "loom-treesitter": 6,
            "loom-scip": 5,
            "baseline-a": 2,
            "baseline-b": 2,
        }
        for _ in range(count):
            order = len(self.entries)
            slot = self.protocol["schedule"][order]
            primary = slot["arm"] in PRIMARY_VIEWS
            ordinal = (
                self.tasks.index(slot["task_id"]) * 3 + slot["repeat"]
                if primary
                else self.tasks.index(slot["task_id"])
            )
            passed = ordinal < successes[slot["arm"]]
            cost = None if unknown_cost else totals[slot["arm"]] / (9 if primary else 3)
            folder = self.source / f"trial-{order:02d}"
            task_config = self.config["tasks"][order]
            trial_config = {
                "task": task_config,
                "trial_name": folder.name,
                "trials_dir": str(self.source),
                "job_id": str(self.source_id),
                "agent": self.config["agents"][0],
            }
            raw = TrialResult.model_validate(
                {
                    "id": str(uuid5(self.source_id, str(order))),
                    "task_name": f"slot-{order:02d}",
                    "trial_name": folder.name,
                    "trial_uri": folder.as_uri(),
                    "task_id": task_config,
                    "task_checksum": hashlib.sha256(str(order).encode()).hexdigest(),
                    "config": trial_config,
                    "agent_info": {
                        "name": "factory",
                        "version": "fixture",
                        "model_info": {"name": "fixture-model"},
                    },
                    "agent_result": {
                        "cost_usd": cost,
                        "n_input_tokens": 100,
                        "n_output_tokens": 20,
                        "n_cache_tokens": 0,
                    },
                    "verifier_result": {"rewards": {"reward": int(passed)}},
                    "started_at": self.start + timedelta(seconds=order * 50),
                    "finished_at": self.start + timedelta(seconds=order * 50 + 36),
                }
            ).model_dump(mode="json")
            lock = {
                "schema_version": 2,
                "task": {
                    "name": raw["task_name"],
                    "type": "local",
                    "digest": "sha256:" + raw["task_checksum"],
                    "path": task_config["path"],
                },
                "agent": self.config["agents"][0],
                "environment": {"type": "docker"},
                "verifier": {"disable": False, "environment_mode": "separate"},
            }
            for name, value in {
                "result.json": raw,
                "config.json": raw["config"],
                "lock.json": lock,
                "agent/trajectory.json": {"fixture": order},
                "agent/factory-result.json": {"resolved": passed},
                "verifier/grade.json": {"passed": passed},
            }.items():
                write(folder / name, value)
            record = {
                "task_id": slot["task_id"],
                "mode": slot["mode"],
                "arm": slot["arm"],
                "index_tier": slot["index_tier"],
                "resolved": passed,
                "harbor_reward_valid": True,
                "generation_cost_usd": cost,
                "end_to_end_s": 37.25,
                "harbor_trial": str(folder),
                "model_selected_loom_requests": 1 if slot["mode"] == "natural" else 0,
                "loom_physical_requests": 3 if slot["mode"] == "natural" else 0,
            }
            self.entries.append({"order": order, "result": record})
            self.raw.append(raw)
            self.locks.append(lock)
        self.sync_job()

    def test_study_export_generates_pending_reports_before_first_trial(self):
        from examples.software_factory.harbor.study import export

        for slot in self.protocol["schedule"]:
            if slot["arm"].startswith("baseline"):
                slot["mode"] = "baseline"
        self.protocol["schedule_sha256"] = digest(self.protocol["schedule"])
        (self.source / "result.json").unlink()
        before = hashes(self.source)
        summary = export(self.protocol)
        self.assertEqual(summary["observed"], 0)
        self.assertEqual(summary["reporting"]["native_views"]["status"], "awaiting_source_job")
        report = read(self.root / "reports/comparison.json")
        self.assertFalse(report["complete"])
        self.assertEqual(report["primary_arms"]["baseline"]["pending"], 9)
        self.assertTrue((self.root / "reports/charts.html").exists())
        self.assertEqual(hashes(self.source), before)

    def test_complete_views_preserve_assets_metrics_and_three_by_nine_pairing(self):
        self.add(33)
        before = hashes(self.source)
        original_entries = copy.deepcopy(self.entries)
        report = generate_reports(self.protocol, self.entries)
        self.assertTrue(report["complete"])
        self.assertEqual(report["scenario_count"], 3)
        self.assertEqual(report["expected_primary_pair_rows"], 9)
        self.assertEqual(report["aa_controls"]["expected"], 6)
        scenario = report["scenarios"]["scenario-a"]
        self.assertEqual(scenario["aa_controls"]["expected"], 2)
        self.assertEqual(set(scenario["aa_control_arms"]), {"baseline-a", "baseline-b"})
        self.assertEqual(report["native_views"]["copied_trials"], 33)
        self.assertFalse(report["charts"]["published"])
        self.assertTrue(Path(report["charts"]["html"]).exists())
        self.assertEqual(report["links"]["charts"], report["charts"]["html"])
        self.assertEqual(read(self.root / "reporting-manifest.json")["charts"], report["charts"])
        for arm, expected in [("baseline", 3), ("loom-treesitter", 6), ("loom-scip", 5)]:
            view = self.root / "jobs" / PRIMARY_VIEWS[arm]
            native = read(view / "result.json")
            self.assertEqual(native["n_total_trials"], 9)
            self.assertEqual(native["stats"]["n_completed_trials"], 9)
            self.assertEqual(report["primary_arms"][arm]["resolved"], expected)
            total = {"baseline": 0.0876721, "loom-treesitter": 0.0875002, "loom-scip": 0.0856215}[
                arm
            ]
            self.assertAlmostEqual(native["stats"]["cost_usd"], total)
            self.assertAlmostEqual(report["primary_arms"][arm]["generation_cost_usd"], total)
        for row in report["paired_rows"]:
            checksums = set()
            for arm in PRIMARY_VIEWS:
                record = row["cells"][arm]
                self.assertIsNotNone(record)
                folder = Path(record["harbor_trial"])
                copied = self.root / "jobs" / PRIMARY_VIEWS[arm] / folder.name
                raw, derived = read(folder / "result.json"), read(copied / "result.json")
                checksums.add(derived["task_checksum"])
                self.assertEqual(
                    {k: v for k, v in derived.items() if k not in {"task_name", "task_checksum"}},
                    {k: v for k, v in raw.items() if k not in {"task_name", "task_checksum"}},
                )
                self.assertEqual(
                    (copied / "agent/trajectory.json").read_bytes(),
                    (folder / "agent/trajectory.json").read_bytes(),
                )
            self.assertEqual(len(checksums), 1)
        self.assertEqual(hashes(self.source), before)
        self.assertEqual(self.entries, original_entries)
        self.assertEqual(len(list((self.root / "reports/attempts").glob("*.json"))), 33)
        self.assertEqual(len(list((self.root / "reports/scenarios").glob("*.json"))), 3)
        self.assertEqual(report["primary_arms"]["baseline"]["mean_native_duration_s"], 36)
        self.assertEqual(report["primary_arms"]["baseline"]["mean_end_to_end_s"], 37.25)

    def test_partial_reports_keep_expected_counts_and_null_cells(self):
        self.add(4)
        report = generate_reports(self.protocol, self.entries)
        self.assertFalse(report["complete"])
        self.assertEqual(report["expected_attempts"], 33)
        self.assertEqual(len(report["pending_orders"]), 29)
        self.assertEqual(report["primary_arms"]["baseline"]["pending"], 8)
        self.assertIsNone(report["primary_arms"]["baseline"]["success_rate"])
        self.assertIsNone(report["paired_rows"][1]["cells"]["loom-scip"])
        self.assertEqual(report["attempts"][4]["state"], "pending")
        self.assertIsNone(report["attempts"][4]["metrics"])
        native = read(self.root / "jobs/loom-treesitter/result.json")
        self.assertEqual(native["n_total_trials"], 9)
        self.assertEqual(native["stats"]["n_completed_trials"], 2)
        self.assertEqual(native["stats"]["n_pending_trials"], 7)
        self.assertIsNone(native["finished_at"])

    def test_empty_run_has_pending_reports_without_fake_trials(self):
        report = generate_reports(self.protocol, [])
        self.assertEqual(report["observed_attempts"], 0)
        self.assertIsNone(report["primary_arms"]["baseline"]["generation_cost_usd"])
        self.assertTrue(
            all(cell is None for row in report["paired_rows"] for cell in row["cells"].values())
        )
        native = read(self.root / "jobs/no-loom/result.json")
        self.assertEqual(native["n_total_trials"], 9)
        self.assertEqual(native["stats"]["n_pending_trials"], 9)
        self.assertIsNone(native["stats"]["cost_usd"])

    def test_before_job_creation_reports_pending_and_recreates_missing_markdown(self):
        (self.source / "result.json").unlink()
        report = generate_reports(self.protocol, [])
        self.assertEqual(report["native_views"]["status"], "awaiting_source_job")
        self.assertEqual(report["pending_orders"], list(range(33)))
        attempt = self.root / "reports/attempts/attempt-00.md"
        attempt.unlink()
        generate_reports(self.protocol, [])
        self.assertTrue(attempt.exists())

    def test_late_source_asset_changes_are_detected(self):
        self.add()
        generate_reports(self.protocol, self.entries)
        write(self.source / "trial-00/agent/late.json", {"late": True})
        with self.assertRaisesRegex(PermissionError, "measured trial assets changed"):
            generate_reports(self.protocol, self.entries)

    def test_incremental_export_copies_only_new_trials_and_keeps_asset_permissions(self):
        self.add(3)
        source_asset = self.source / "trial-00/agent/trajectory.json"
        mode = source_asset.stat().st_mode
        first = generate_reports(self.protocol, self.entries)
        self.assertEqual(first["native_views"]["copied_trials"], 3)
        asset = self.root / "jobs/no-loom/trial-00/agent/trajectory.json"
        before = asset.stat().st_mtime_ns
        second = generate_reports(self.protocol, self.entries)
        self.assertEqual(second["native_views"]["copied_trials"], 0)
        self.add()
        third = generate_reports(self.protocol, self.entries)
        self.assertEqual(third["native_views"]["copied_trials"], 1)
        self.assertEqual(asset.stat().st_mtime_ns, before)
        self.assertEqual(source_asset.stat().st_mode, mode)

    def test_cached_asset_mutation_is_detected_without_touching_measured_assets(self):
        self.add()
        generate_reports(self.protocol, self.entries)
        before = hashes(self.source)
        (self.root / "jobs/no-loom/trial-00/agent/trajectory.json").write_text("changed")
        with self.assertRaisesRegex(PermissionError, "asset changed"):
            generate_reports(self.protocol, self.entries)
        self.assertEqual(hashes(self.source), before)

    def test_interrupted_copy_is_recovered_without_a_partial_native_trial(self):
        self.add()
        with patch(
            "examples.software_factory.harbor.reporting.shutil.copytree",
            side_effect=OSError("copy stopped"),
        ):
            with self.assertRaises(OSError):
                generate_reports(self.protocol, self.entries)
        self.assertFalse((self.root / "jobs/no-loom/trial-00").exists())
        recovered = generate_reports(self.protocol, self.entries)
        self.assertEqual(recovered["native_views"]["copied_trials"], 1)
        self.assertTrue((self.root / "jobs/no-loom/trial-00/result.json").exists())

    def test_unknown_cost_and_counters_stay_unknown(self):
        self.add(unknown_cost=True)
        self.entries[0]["result"]["model_selected_loom_requests"] = None
        report = generate_reports(self.protocol, self.entries)
        self.assertIsNone(report["primary_arms"]["baseline"]["generation_cost_usd"])
        self.assertIsNone(report["primary_arms"]["baseline"]["selected_loom_calls"])
        self.assertIsNone(read(self.root / "jobs/no-loom/result.json")["stats"]["cost_usd"])

    def test_central_viewer_root_is_configurable_and_unrelated_jobs_are_refused(self):
        central = self.root.parent / "viewer-jobs"
        self.protocol["reporting"] = {
            "viewer_jobs_root": str(central),
            "viewer_base_url": "http://localhost:9000",
            "job_prefix": "study-",
            "publish_charts": False,
        }
        self.add()
        report = generate_reports(self.protocol, self.entries)
        self.assertTrue((central / "study-no-loom/result.json").exists())
        self.assertTrue(report["links"]["comparison"].startswith("http://localhost:9000/compare?"))
        self.protocol["reporting"]["job_prefix"] = "unrelated-"
        write(central / "unrelated-no-loom/result.json", {"keep": True})
        with self.assertRaisesRegex(PermissionError, "unrelated Harbor job"):
            generate_reports(self.protocol, self.entries)
        self.assertEqual(read(central / "unrelated-no-loom/result.json"), {"keep": True})
