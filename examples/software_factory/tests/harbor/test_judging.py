import copy
import hashlib
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from examples.software_factory.evals.flask.grading import validate_candidate
from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.arms import PRIMARY
from examples.software_factory.harbor.judging import (
    _file_patch,
    blind_input,
    parse_verdict,
    run_judging,
)
from examples.software_factory.harbor.one_shot import spending_summary


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
            for i in range(len(PRIMARY))
        ]
        for candidate in self.candidates:
            candidate["state"] = digest(candidate["files"])
        self.schedule = [
            {"order": i, "task_id": "issue", "repeat": 0, "arm": arm}
            for i, arm in enumerate(PRIMARY)
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
            "judge": {
                "enabled": True,
                "seed": 741,
                "model_id": "claude-haiku-5-5",
                "runtime": "claude-code",
            },
        }
        self.history = self.root / "public-history.json"
        self.history.write_text(json.dumps({"public_documents": {}}))
        self.protocol["memory_histories"] = {
            "issue": {
                "fixture_file": str(self.history),
                "fixture_sha256": hashlib.sha256(self.history.read_bytes()).hexdigest(),
            }
        }
        self.entries = []
        for i, candidate in enumerate(self.candidates):
            folder = self.root / f"trial-{i}"
            (folder / "verifier").mkdir(parents=True)
            (folder / "verifier/candidate.json").write_text(json.dumps(candidate))
            self.entries.append({"order": i, "result": {"harbor_trial": str(folder)}})

    def response(self):
        return {
            "structured_output": {
                "fixed": {"A": True, "B": None, "C": False, "D": True, "E": False},
                "reason": "Source assessment.",
            },
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
        self.assertEqual(
            set(visible),
            {"public_issue", "public_documents", "base_files", "A", "B", "C", "D", "E", "context"},
        )
        self.assertNotIn("must stay private", json.dumps(visible))
        self.assertEqual({entry["order"] for entry in mapping.values()}, set(range(5)))
        self.assertEqual(self.candidates, before)
        path = "src/flask/app.py"
        self.assertEqual(visible["base_files"], self.task.files)
        for label, entry in mapping.items():
            patch = visible[label][path]
            self.assertEqual(patch["change"], "modified")
            self.assertIn("-value = 1\n", patch["patch"])
            self.assertIn("+" + before[entry["order"]]["files"][path], patch["patch"])

    def test_diff_keeps_special_path_characters_in_quoted_headers(self):
        patch = _file_patch("src/flask/with\ttab\nnewline.py", "x = 1\n", "x = 2\n")
        self.assertIn(r'--- "a/src/flask/with\011tab\012newline.py"', patch["patch"])
        self.assertIn(r'+++ "b/src/flask/with\011tab\012newline.py"', patch["patch"])

    def test_structured_verdict_preserves_unknown_and_rejects_nonboolean_or_multiple(self):
        self.assertIsNone(parse_verdict(self.response(), "ABCDE")["fixed"]["B"])
        invalid = self.response()
        invalid["structured_output"]["fixed"]["A"] = 1
        with self.assertRaises(ValueError):
            parse_verdict(invalid, "ABCDE")
        invalid = self.response()
        invalid["structured_output"] = [self.response()["structured_output"]] * 2
        with self.assertRaises(ValueError):
            parse_verdict(invalid, "ABCDE")

    def test_every_blind_candidate_shares_only_the_frozen_public_documents(self):
        self.history.write_text(
            json.dumps(
                {
                    "public_documents": {
                        "public-policy": "Preserve the documented zero-value behavior."
                    },
                    "private_working_notes": "HIDDEN_WORKING_SENTINEL",
                }
            )
        )
        self.protocol["memory_histories"]["issue"]["fixture_sha256"] = hashlib.sha256(
            self.history.read_bytes()
        ).hexdigest()
        judge = Mock(return_value=self.response())
        with patch(
            "examples.software_factory.harbor.judging.task_inputs",
            return_value=(None, None, self.task, None),
        ):
            run_judging(self.protocol, self.entries, judge=judge)
        blocks = judge.call_args.args[2]
        text = "\n".join(block["text"] for block in blocks)
        self.assertIn("Preserve the documented zero-value behavior.", text)
        self.assertNotIn("HIDDEN_WORKING_SENTINEL", text)
        self.assertNotIn("must stay private", text)
        base = next(
            block["text"].split("\n", 1)[1]
            for block in blocks[1:]
            if json.loads(block["text"].split("\n", 1)[0])["section"] == "base_file"
        )
        self.assertEqual(base, self.task.files["src/flask/app.py"])

    def test_missing_or_mutated_history_refuses_before_judge_dispatch(self):
        judge = Mock(return_value=self.response())
        self.history.write_text('{"public_documents": {"changed": "data"}}')
        with patch(
            "examples.software_factory.harbor.judging.task_inputs",
            return_value=(None, None, self.task, None),
        ):
            with self.assertRaises(PermissionError):
                run_judging(self.protocol, self.entries, judge=judge)
        judge.assert_not_called()

    def test_owned_judge_receives_the_frozen_protocol_and_verdict_schema(self):
        @contextmanager
        def assess(*_arguments):
            yield self.response()
            receipt = self.root / "judgements/pair-00.json"
            self.assertEqual(json.loads(receipt.read_text())["generation_cost_usd"], 0.01)

        meter = Mock()
        meter.summary.return_value = {"complete": True}
        with (
            patch(
                "examples.software_factory.harbor.judging.native_judge",
                side_effect=assess,
            ) as judge,
            patch(
                "examples.software_factory.harbor.judging.task_inputs",
                return_value=(None, None, self.task, None),
            ),
        ):
            run_judging(self.protocol, self.entries, meter=meter)
        judge.assert_called_once()
        protocol, task_id, _sections, schema = judge.call_args.args
        self.assertIs(protocol, self.protocol)
        self.assertEqual(task_id, "issue")
        self.assertEqual(set(schema["properties"]["fixed"]["required"]), set("ABCDE"))

    def test_unsettled_meter_blocks_native_dispatch_before_creating_intent(self):
        meter = Mock()
        meter.summary.return_value = {"complete": False}
        with patch("examples.software_factory.harbor.judging.native_judge") as judge:
            with self.assertRaisesRegex(PermissionError, "Unsettled provider billing"):
                run_judging(self.protocol, self.entries, meter=meter)
        judge.assert_not_called()
        self.assertEqual(list((self.root / "judgements").glob("*.intent.json")), [])

    def test_partial_judge_receipts_remain_known_when_another_pair_is_unsettled(self):
        study = self.root / "study"
        folder = study / "judgements"
        folder.mkdir(parents=True)
        (study / "protocol.json").write_text('{"schedule": [{}]}')
        (study / "live-attempts.json").write_text('[{"result": {"generation_cost_usd": 0.03}}]')
        (folder / "pair-00.intent.json").touch()
        (folder / "pair-00.json").write_text('{"generation_cost_usd": 0.01}')
        meter = Mock()
        meter.summary.return_value = {"spent_usd": 0.02, "complete": True}
        summary = spending_summary(meter, self.root)
        self.assertEqual(summary["native_judge"]["status"], "partial")
        self.assertAlmostEqual(summary["total_provider_cost_usd"], 0.06)
        (folder / "pair-01.intent.json").touch()
        summary = spending_summary(meter, self.root)
        self.assertEqual(summary["native_judge"]["status"], "unsettled")
        self.assertEqual(summary["native_judge"]["reported_cost_usd"], 0.01)
        self.assertIsNone(summary["total_provider_cost_usd"])

    def test_judging_is_once_per_complete_pair_with_independent_cost_and_no_raw_edits(self):
        judge = Mock(return_value=self.response())
        before = {p: p.read_bytes() for p in self.root.glob("trial-*/verifier/candidate.json")}
        with patch(
            "examples.software_factory.harbor.judging.task_inputs",
            return_value=(None, None, self.task, None),
        ):
            result = run_judging(self.protocol, self.entries, judge=judge)
            repeated = run_judging(self.protocol, self.entries, judge=judge)
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(result["generation_cost_usd"], 0.01)
        self.assertEqual(result["attempts"], repeated["attempts"])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        sent = judge.call_args.args[2]
        self.assertNotIn("hidden_grade", json.dumps(sent))

    def test_incomplete_cohort_and_interrupted_intent_never_dispatch_automatically(self):
        judge = Mock()
        result = run_judging(self.protocol, self.entries[:1], judge=judge)
        self.assertEqual(result["status"], "awaiting_complete_cohort")
        folder = self.root / "judgements"
        folder.mkdir()
        (folder / "pair-00.intent.json").write_text("{}")
        with self.assertRaises(PermissionError):
            run_judging(self.protocol, self.entries, judge=judge)
        judge.assert_not_called()
