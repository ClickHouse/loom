import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from examples.software_factory.evals.flask.grading import validate_candidate, validate_fixtures
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.index import MUTABLE_STATE_KEYS, TABLES, fingerprint
from examples.software_factory.harbor.study import result_record


class HarborContracts(unittest.TestCase):
    def test_trusted_fixtures_preserve_bytes_without_expanding_candidate_authority(self):
        fixtures = {"tests/app/.env": base64.b64encode(b"VALUE=fixture\n").decode()}
        self.assertEqual(
            validate_fixtures(fixtures, {}, digest(fixtures)),
            {"tests/app/.env": b"VALUE=fixture\n"},
        )
        for name in ("../outside", ".git/config", "src/flask/app.py"):
            invalid = {name: base64.b64encode(b"fixture").decode()}
            with self.assertRaises(PermissionError):
                validate_fixtures(invalid, {}, digest(invalid))
        with self.assertRaises(PermissionError):
            validate_fixtures(fixtures, {"tests/app/.env": "base"}, digest(fixtures))
        with self.assertRaises(PermissionError):
            validate_fixtures(fixtures, {}, "0" * 64)

    def test_only_named_mutable_state_is_excluded_from_source_fingerprint(self):
        source = {t: [] for t in TABLES}
        row = {"org": "org", "namespace": "flask", "state_key": "git_head", "value": "base"}
        source["code_index_state"] = [row]
        expected = fingerprint(source)
        for key in MUTABLE_STATE_KEYS:
            source["code_index_state"].append({**row, "state_key": key, "value": "cached"})
        self.assertEqual(fingerprint(source), expected)
        source["code_index_state"].append({**row, "state_key": "unknown_future_state"})
        self.assertNotEqual(fingerprint(source), expected)
        source["code_index_state"] = [{**row, "value": "changed-base"}]
        self.assertNotEqual(fingerprint(source), expected)
        source["code_index_state"] = [row]
        source["code_symbols"] = [{"org": "org", "namespace": "flask", "name": "changed"}]
        self.assertNotEqual(fingerprint(source), expected)

    def test_verifier_rejects_test_injection_missing_files_and_wrong_base(self):
        base = {"src/flask/app.py": "base", "tests/test_app.py": "private"}
        files = {"src/flask/app.py": "candidate"}
        candidate = {
            "source_sha256": digest({"src/flask/app.py": "base"}),
            "files": files,
            "state": digest(files),
        }
        self.assertEqual(validate_candidate(candidate, base, candidate["source_sha256"]), files)
        for change in ({"tests/test_app.py": "pass"}, {}):
            altered = {**candidate, "files": change, "state": digest(change)}
            with self.assertRaises(PermissionError):
                validate_candidate(altered, base, candidate["source_sha256"])
        with self.assertRaises(PermissionError):
            validate_candidate(candidate, base, "different-base")

    def test_infrastructure_failure_retains_spend_and_cannot_be_a_success(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "agent").mkdir()
            result = {
                "task_id": "flask",
                "mode": "baseline",
                "status": "completed",
                "resolved": False,
                "generation_cost_usd": 0.02,
            }
            (folder / "agent/factory-result.json").write_text(json.dumps(result))
            raw = {
                "reward_valid": False,
                "verifier_result": {"rewards": {"reward": 1}},
                "started_at": "2000-01-01T00:00:00+00:00",
                "finished_at": "2000-01-01T00:00:12+00:00",
            }
            row = result_record(
                {"task_id": "flask", "mode": "baseline", "order": 0, "repeat": 0},
                raw,
                folder,
                task_kind="historical-flask",
            )
            self.assertFalse(row["resolved"])
            self.assertEqual(row["generation_cost_usd"], 0.02)
            self.assertEqual(row["end_to_end_s"], 12)
            self.assertEqual(row["status"], "infrastructure_error")

    def test_factory_defers_hidden_checks_to_harbor(self):
        from examples.software_factory.tests.factory.test_factory import FactoryContracts

        fixture = FactoryContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        factory = fixture.factory(mode="baseline")
        original = factory.grader.run

        def visible_only(checkout, *, hidden=False):
            self.assertFalse(hidden, "Agent attempted to access its hidden verifier")
            return original(checkout)

        factory.grader.run = visible_only
        result = factory.run(external_verifier=True)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["visible_passed"])
        self.assertFalse(result["resolved"])
        self.assertTrue(result["hidden_grade"]["deferred"])

    def test_atif_retains_every_tool_call_and_matching_observation(self):
        from examples.software_factory.harbor.agent import trajectory

        factory = Mock(
            messages=[
                {"role": "user", "content": [{"text": "fix"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"toolUse": {"toolUseId": "a", "name": "read", "input": {"path": "app.py"}}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "a",
                                "content": [{"json": {"content": "source"}}],
                            }
                        }
                    ],
                },
            ]
        )
        doc = trajectory(
            factory,
            {"mode": "baseline", "model_selected_loom_requests": 0},
            "model",
        )
        self.assertEqual(doc["steps"][1]["tool_calls"][0]["tool_call_id"], "a")
        self.assertEqual(doc["steps"][1]["observation"]["results"][0]["source_call_id"], "a")
