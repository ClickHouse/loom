import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from examples.software_factory.evals.flask.grading import validate_candidate
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.judging import blind_input, parse_verdict, run_judging


class BlindJudging(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task = SimpleNamespace(
            problem="Reject invalid input and preserve valid behavior.",
            files={"src/flask/app.py": "value = 1\n"},
        )
        self.task.source_sha256 = digest(self.task.files)
        self.candidates = [
            {
                "source_sha256": self.task.source_sha256,
                "files": {"src/flask/app.py": f"value = {i + 2}\n"},
                "hidden_grade": "must stay private",
                "arm": "must stay private",
            }
            for i in range(3)
        ]
        for candidate in self.candidates:
            candidate["state"] = digest(candidate["files"])
        self.schedule = [
            {"order": i, "task_id": "issue", "repeat": 0, "arm": arm}
            for i, arm in enumerate(("baseline", "loom-treesitter", "loom-scip"))
        ]
        self.protocol = {
            "suite": {"adapter": "examples.software_factory.evals.flask.profile"},
            "controller": "harbor",
            "harbor_version": "0.27.0",
            "output_root": str(self.root),
            "files": {},
            "schedule": self.schedule,
            "schedule_sha256": digest(self.schedule),
            "model_id": "claude-haiku-5-5",
            "index_tier": "per-arm",
            "judge": {"enabled": True, "seed": 741, "output_tokens": 4096},
        }
        self.entries = []
        for i, candidate in enumerate(self.candidates):
            folder = self.root / f"trial-{i}"
            (folder / "agent").mkdir(parents=True)
            (folder / "agent/candidate.json").write_text(json.dumps(candidate))
            self.entries.append({"order": i, "result": {"harbor_trial": str(folder)}})

    def response(self):
        return {
            "blocks": [
                {
                    "toolUse": {
                        "name": "record_verdict",
                        "input": {
                            "fixed": {"A": True, "B": None, "C": False},
                            "reason": "Source assessment.",
                        },
                    }
                }
            ],
            "generation_cost_usd": 0.01,
        }

    def test_judge_sees_only_public_issue_base_and_anonymous_candidate_source(self):
        before = copy.deepcopy(self.candidates)
        visible, mapping = blind_input(
            self.task,
            list(enumerate(self.candidates)),
            seed=741,
            validate_candidate=validate_candidate,
        )
        self.assertEqual(set(visible), {"public_issue", "base_files", "A", "B", "C", "context"})
        self.assertNotIn("must stay private", json.dumps(visible))
        self.assertEqual({entry["order"] for entry in mapping.values()}, {0, 1, 2})
        self.assertEqual(self.candidates, before)

    def test_structured_verdict_preserves_unknown_and_rejects_nonboolean_or_multiple(self):
        self.assertIsNone(parse_verdict(self.response(), "ABC")["fixed"]["B"])
        invalid = self.response()
        invalid["blocks"][0]["toolUse"]["input"]["fixed"]["A"] = 1
        with self.assertRaises(ValueError):
            parse_verdict(invalid, "ABC")
        invalid = self.response()
        invalid["blocks"] *= 2
        with self.assertRaises(ValueError):
            parse_verdict(invalid, "ABC")

    def test_judging_is_once_per_complete_pair_with_independent_cost_and_no_raw_edits(self):
        provider = Mock(step=Mock(return_value=self.response()))
        before = {p: p.read_bytes() for p in self.root.glob("trial-*/agent/candidate.json")}
        with patch(
            "examples.software_factory.harbor.judging.task_inputs",
            return_value=(None, None, self.task, None),
        ):
            result = run_judging(self.protocol, self.entries, provider=provider)
            repeated = run_judging(self.protocol, self.entries, provider=provider)
        self.assertEqual(provider.step.call_count, 1)
        self.assertEqual(result["generation_cost_usd"], 0.01)
        self.assertEqual(result["attempts"], repeated["attempts"])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        sent = provider.step.call_args.args[0]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("hidden_grade", json.dumps(sent))

    def test_incomplete_cohort_and_interrupted_intent_never_dispatch_automatically(self):
        provider = Mock()
        result = run_judging(self.protocol, self.entries[:1], provider=provider)
        self.assertEqual(result["status"], "awaiting_complete_cohort")
        folder = self.root / "judgements"
        folder.mkdir()
        (folder / "pair-00.intent.json").write_text("{}")
        with self.assertRaises(PermissionError):
            run_judging(self.protocol, self.entries, provider=provider)
        provider.step.assert_not_called()
