"""Host comparisons reject candidate-written verdicts and malformed observations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.software_factory.adapters import ReplayProvider
from examples.software_factory.behavioral import BehavioralGrader, assess, probe_suite
from examples.software_factory.engine import Factory
from examples.software_factory.model import Task, digest
from examples.software_factory.observations import CandidateFailure, DockerObservations


class BehavioralContracts(unittest.TestCase):
    def setUp(self):
        self.seed = "a" * 64

    def test_all_three_suites_detect_wrong_behavior_without_candidate_verdicts(self):
        for case in ("4045", "4575", "4935"):
            with self.subTest(case=case):
                probes = probe_suite("pallets__flask-" + case, self.seed)
                rows = [{"id": p.key, "value": p.expected} for p in probes]
                self.assertTrue(assess(probes, json.dumps(rows).encode())["passed"])
                rows[-1]["value"] = {"passed": True}
                self.assertFalse(assess(probes, json.dumps(rows).encode())["passed"])
                self.assertTrue(any(p.visible for p in probes))
                self.assertTrue(any(not p.visible for p in probes))
                for p in probes:
                    self.assertEqual(set(p.request), {"id", "op", "args"})
                    self.assertNotIn("expected", p.request)

    def test_exit_zero_or_forged_grade_never_substitutes_for_observations(self):
        probes = probe_suite("pallets__flask-4045", self.seed)
        for forged in (b"", b'{"passed":true}', b'<testsuite tests="9"/>', b"[]"):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                assess(probes, forged)

    def test_missing_duplicate_extra_replayed_and_oversized_observations_refuse(self):
        probes = probe_suite("pallets__flask-4935", self.seed)
        rows = [{"id": p.key, "value": p.expected} for p in probes]
        malformed = [rows[:-1], rows + [rows[0]], [{**r, "passed": True} for r in rows]]
        malformed.append([{**r, "id": "replayed"} for r in rows])
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValueError):
                assess(probes, json.dumps(value).encode())
        for raw in (b" " * 65537, b'[{"id":"a","id":"b","value":{}}]', b"[NaN]"):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                assess(probes, raw)

    def test_seed_freezes_paired_inputs_and_changes_replay_ids(self):
        first = probe_suite("pallets__flask-4045", self.seed)
        self.assertEqual(first, probe_suite("pallets__flask-4045", self.seed))
        other = probe_suite("pallets__flask-4045", "b" * 64)
        self.assertNotEqual([p.request for p in first], [p.request for p in other])
        with self.assertRaises(ValueError):
            probe_suite("unknown", self.seed)
        with self.assertRaises(ValueError):
            probe_suite("pallets__flask-4045", "not-frozen")

    def test_grader_keeps_assertions_outside_container_and_refuses_wrong_base(self):
        base = {"src/flask/__init__.py": "# fixture\n"}
        source = SimpleNamespace(commit="a" * 40, files=lambda prefix: dict(base))
        checkout = SimpleNamespace(original=base, files=lambda: dict(base), state=lambda: "state")
        with tempfile.TemporaryDirectory() as directory:
            grader = BehavioralGrader(
                source,
                "pallets__flask-4045",
                "sha256:" + "a" * 64,
                self.seed,
                Path(directory) / "grading",
            )

            def observe(_runner, _workspace, requests):
                self.assertTrue(all(set(r) == {"id", "op", "args"} for r in requests))
                selected = {r["id"] for r in requests}
                probes = probe_suite("pallets__flask-4045", self.seed)
                return json.dumps(
                    [{"id": p.key, "value": p.expected} for p in probes if p.key in selected]
                ).encode()

            with patch.object(DockerObservations, "run", autospec=True, side_effect=observe):
                visible = grader.run(checkout)
                self.assertTrue(visible["passed"])
                self.assertEqual(visible["result_authority"], "host")
                hidden = grader.run(checkout, hidden=True)
                self.assertTrue(hidden["passed"])
                self.assertNotIn("selected", hidden)
                checkout.original = {}
                with self.assertRaises(PermissionError):
                    grader.run(checkout, hidden=True)

    def test_container_exposes_no_writable_report_or_grader_mount(self):
        runner = DockerObservations("sha256:" + "a" * 64, Path("/unused"))
        command = runner.command(Path("/candidate"), "owned", "owner")
        joined = " ".join(command)
        for required in ("--network=none", "--read-only", "--cap-drop=ALL", "--pull=never"):
            self.assertIn(required, command)
        self.assertIn("target=/workspace,readonly", joined)
        self.assertNotIn("/results", joined)
        self.assertNotIn("behavioral.py", joined)
        self.assertEqual(command[-2:], ["python", "/bridge.py"])

    def factory(self, root, steps):
        files = {"src/flask/__init__.py": "broken\n"}
        source = SimpleNamespace(commit="a" * 40, files=lambda prefix: dict(files))
        task = Task(
            "authored-bridge-fixture",
            "authored-bridge-fixture",
            "authored/bridge",
            "a" * 40,
            "fixture",
            "Repair the authored fixture",
            "unused",
            files,
            digest(files),
        )
        grader = BehavioralGrader(
            source, "pallets__flask-4045", "sha256:" + "a" * 64, self.seed, root / "grading"
        )
        replay = root / "replay.json"
        replay.write_text(json.dumps({"steps": steps}))
        return Factory(task, ReplayProvider(replay), grader, root / "agent", mode="baseline")

    def test_candidate_failure_allows_test_edit_test_revision(self):
        steps = [
            {"actions": [{"name": "test"}]},
            {
                "actions": [
                    {
                        "name": "edit",
                        "input": {"path": "src/flask/__init__.py", "old": "broken", "new": "valid"},
                    },
                    {"name": "test"},
                ]
            },
            {"actions": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            factory = self.factory(Path(directory), steps)

            def observe(_runner, workspace, requests):
                if "broken" in (workspace / "src/flask/__init__.py").read_text():
                    raise CandidateFailure("candidate_exit", "SyntaxError: authored fixture")
                expected = {p.key: p.expected for p in factory.grader.probes}
                return json.dumps(
                    [{"id": r["id"], "value": expected[r["id"]]} for r in requests]
                ).encode()

            with patch.object(DockerObservations, "run", autospec=True, side_effect=observe):
                result = factory.run()
            self.assertTrue(result["resolved"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                [r["passed"] for r in factory.ledger.rows if r["kind"] == "verification"],
                [False, True],
            )

    def test_crash_or_forged_output_is_unresolved_without_invalidating_oracle(self):
        for response in (
            CandidateFailure("candidate_exit", "ImportError: fixture"),
            b'{"passed":true}',
        ):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                factory = self.factory(Path(directory), [{"actions": []}])
                kwargs = (
                    {"side_effect": response}
                    if isinstance(response, Exception)
                    else {"return_value": response}
                )
                with patch.object(DockerObservations, "run", **kwargs):
                    result = factory.run()
                self.assertFalse(result["resolved"])
                self.assertEqual(result["status"], "completed")

    def test_transport_failure_still_invalidates_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            factory = self.factory(Path(directory), [{"actions": []}])
            with patch.object(
                DockerObservations, "run", side_effect=RuntimeError("Docker transport failed")
            ):
                result = factory.run()
            self.assertEqual(result["status"], "invalid_grader")

    def test_visible_infrastructure_failure_remains_invalid_after_hidden_success(self):
        for actions in ([], [{"name": "test"}]):
            for failure in (RuntimeError("transport failed"), ValueError("invalid runtime")):
                with self.subTest(actions=actions, failure=failure):
                    with tempfile.TemporaryDirectory() as directory:
                        factory = self.factory(Path(directory), [{"actions": actions}])
                        with patch.object(
                            factory.grader,
                            "run",
                            side_effect=[failure, {"passed": True}],
                        ) as grade:
                            result = factory.run()
                        self.assertEqual(grade.call_count, 2)
                        self.assertTrue(grade.call_args.kwargs["hidden"])
                        self.assertEqual(result["status"], "invalid_grader")
                        self.assertFalse(result["resolved"])
                        self.assertIn(str(failure), result["error"])

    def test_attach_output_limit_is_candidate_failure_before_runtime_mismatch(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                workspace.mkdir()
                runner = DockerObservations("sha256:" + "a" * 64, root / "observation")

                def command(args, observed_stream=stream, **kwargs):
                    if args[1] == "create":
                        return SimpleNamespace(returncode=0)
                    if args[1] == "start":
                        kwargs[observed_stream].write(b"x" * (1024 * 1024))
                        return SimpleNamespace(returncode=-25)
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "ExitCode": 0,
                                "Running": True,
                                "OOMKilled": False,
                                "Error": "",
                            }
                        ),
                    )

                with patch(
                    "examples.software_factory.observations.subprocess.run", side_effect=command
                ):
                    with patch("examples.software_factory.observations.remove_owned") as cleanup:
                        with self.assertRaises(CandidateFailure) as failure:
                            runner.run(
                                workspace, [{"id": "fixture", "op": "blueprint", "args": {}}]
                            )
                self.assertEqual(failure.exception.reason, "candidate_output_limit")
                cleanup.assert_called_once()
                runtime = json.loads((runner.output / "runtime.json").read_text())
                self.assertEqual(runtime["output_bytes"][stream], 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
