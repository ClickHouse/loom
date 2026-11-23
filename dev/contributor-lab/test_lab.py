"""Exercise the trusted publisher without network access or native toolchains."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/contributor-lab.yml"
_BOT = {"login": "github-actions[bot]"}


class _API:
    def __init__(self) -> None:
        self.issues: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.requests: list[tuple[str, str]] = []

    def run(self, command, **kwargs):
        method, endpoint = command[3:5]
        assert command[:3] == ["gh", "api", "--method"]
        assert endpoint.startswith("repos/ClickHouse/loom/issues")
        self.requests.append((method, endpoint))
        data = json.loads(kwargs["input"]) if kwargs.get("input") else None
        if method == "GET":
            rows = (
                self.comments[int(endpoint.split("/")[-2])]
                if "/comments?" in endpoint
                else self.issues
            )
            return SimpleNamespace(stdout=json.dumps(rows))
        assert method == "POST" and data is not None
        if endpoint.endswith("/comments"):
            number = int(endpoint.split("/")[-2])
            row = {"id": len(self.comments[number]) + 1, "created_at": "fixed", "user": _BOT}
            row.update(data)
            self.comments[number].append(row)
        else:
            number = len(self.issues) + 1
            row = {"number": number, "user": _BOT, "state": "open", **data}
            self.issues.append(row)
            self.comments[number] = []
        return SimpleNamespace(stdout=json.dumps(row))


class LabPublisher(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _API()
        self.results = {key: {"result": "success"} for key in ("source_container",)}
        text = _WORKFLOW.read_text().rsplit("          python3 - <<'PY'\n", 1)[1]
        self.source = "\n".join(line[10:] for line in text.splitlines() if line != "          PY")

    def publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "LAB_RESULTS": json.dumps(self.results),
                "GITHUB_RUN_ID": "1234",
                "GITHUB_STEP_SUMMARY": str(Path(directory) / "summary"),
            }
            with patch.dict(os.environ, env, clear=True), patch("subprocess.run", self.api.run):
                exec(compile(self.source, str(_WORKFLOW), "exec"), {})

    def test_success_does_not_create_a_task(self) -> None:
        self.publish()
        self.assertEqual(self.api.issues, [])
        self.assertEqual(len(self.api.requests), 1)

    def test_repeated_failure_has_one_task_without_comments(self) -> None:
        self.results["source_container"]["result"] = "failure"
        self.publish()
        self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.comments[1], [])

    def test_success_and_reappearance_each_append_one_status(self) -> None:
        self.results["source_container"]["result"] = "failure"
        self.publish()
        self.results["source_container"]["result"] = "success"
        self.publish()
        self.publish()
        self.results["source_container"]["result"] = "failure"
        self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.comments[1]), 2)

    def test_human_body_replacement_gets_one_recovery_status(self) -> None:
        self.results["source_container"]["result"] = "failure"
        self.publish()
        self.api.issues[0]["body"] = "Human diagnosis"
        self.publish()
        self.publish()
        self.assertEqual(self.api.issues[0]["body"], "Human diagnosis")
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.comments[1]), 1)

    def test_closed_decision_is_preserved(self) -> None:
        self.results["source_container"]["result"] = "failure"
        self.publish()
        self.api.issues[0]["state"] = "closed"
        self.publish()
        self.assertEqual(self.api.issues[0]["state"], "closed")
        self.assertEqual(self.api.comments[1], [])

    def test_cancelled_and_skipped_jobs_create_no_task(self) -> None:
        self.results["source_container"]["result"] = "cancelled"
        self.publish()
        self.results["source_container"]["result"] = "skipped"
        self.publish()
        self.assertEqual(self.api.issues, [])

    def test_full_comment_window_refuses_without_another_write(self) -> None:
        self.results["source_container"]["result"] = "failure"
        self.publish()
        self.api.comments[1] = [{}] * 100
        with self.assertRaisesRegex(AssertionError, "history budget"):
            self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.comments[1]), 100)

    def test_api_refusal_propagates_without_retry(self) -> None:
        def refuse(command, **kwargs):
            self.api.requests.append(("GET", "refused"))
            raise subprocess.CalledProcessError(1, command)

        self.api.run = refuse
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(self.api.issues, [])

    def test_duplicate_identity_refuses_before_a_write(self) -> None:
        self.results["source_container"]["result"] = "failure"
        self.publish()
        self.api.issues.append(dict(self.api.issues[0]))
        writes = sum(method == "POST" for method, _ in self.api.requests)
        with self.assertRaisesRegex(AssertionError, "Duplicate source lab"):
            self.publish()
        self.assertEqual(sum(method == "POST" for method, _ in self.api.requests), writes)


class SelectedCheckReports(unittest.TestCase):
    def setUp(self) -> None:
        path = Path(__file__).with_name("check.py")
        spec = importlib.util.spec_from_file_location("lab_check", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_zero_skipped_missing_and_nonzero_process_reports_fail(self) -> None:
        for total, skipped, status, exists, expected in [
            (2, 0, 0, True, True),
            (0, 0, 0, True, False),
            (2, 2, 0, True, False),
            (2, 0, 1, True, False),
            (2, 0, 0, False, False),
        ]:
            with self.subTest(total=total, skipped=skipped, status=status, exists=exists):
                with tempfile.TemporaryDirectory() as directory:

                    def run(
                        command,
                        total=total,
                        skipped=skipped,
                        status=status,
                        exists=exists,
                        **kwargs,
                    ):
                        if exists:
                            report = next(
                                arg.split("=", 1)[1]
                                for arg in command
                                if arg.startswith("--junitxml=")
                            )
                            Path(report).write_text(
                                f'<testsuites><testsuite tests="{total}" '
                                f'skipped="{skipped}"/></testsuites>'
                            )
                        return SimpleNamespace(returncode=status)

                    with patch("subprocess.run", run):
                        result = self.module.check("functional", Path(directory))
                    self.assertEqual(result["passed"], expected)

    def test_a_previous_successful_report_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / "functional.xml"
            previous.write_text('<testsuites><testsuite tests="2" skipped="0"/></testsuites>')
            with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)):
                result = self.module.check("functional", Path(directory))
            self.assertFalse(result["passed"])
            self.assertEqual(result["tests"], 0)

    def test_one_failed_quality_check_fails_the_track(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "subprocess.run",
                side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=1)],
            ):
                self.assertFalse(self.module.check("quality", Path(directory))["passed"])

    def test_unknown_track_is_rejected_before_execution(self) -> None:
        with patch("sys.argv", ["check.py", "untrusted-command"]), patch("subprocess.run") as run:
            with self.assertRaises(SystemExit) as error:
                self.module.main()
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
