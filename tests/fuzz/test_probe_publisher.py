"""Exercise the probe publisher without network access or a fuzzing engine."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/identity-testing.yml"
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


class ProbePublisher(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _API()
        self.results = {
            key: {"result": "success", "outputs": {"review_count": "0"}}
            for key in ("replay_and_mutation", "coverage_guided")
        }
        text = _WORKFLOW.read_text().rsplit("          python3 - <<'PY'\n", 1)[1]
        self.source = "\n".join(line[10:] for line in text.splitlines() if line != "          PY")

    def publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "PROBE_RESULTS": json.dumps(self.results),
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
        self.results["coverage_guided"]["result"] = "failure"
        self.publish()
        self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.comments[1], [])

    def test_success_and_reappearance_each_append_one_status(self) -> None:
        self.results["replay_and_mutation"]["result"] = "failure"
        self.publish()
        self.results["replay_and_mutation"]["result"] = "success"
        self.publish()
        self.publish()
        self.results["replay_and_mutation"]["result"] = "failure"
        self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.comments[1]), 2)

    def test_human_body_replacement_gets_one_recovery_status(self) -> None:
        self.results["coverage_guided"]["result"] = "failure"
        self.publish()
        self.api.issues[0]["body"] = "Human diagnosis"
        self.publish()
        self.publish()
        self.assertEqual(self.api.issues[0]["body"], "Human diagnosis")
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.comments[1]), 1)

    def test_closed_decision_is_preserved(self) -> None:
        self.results["replay_and_mutation"]["result"] = "failure"
        self.publish()
        self.api.issues[0]["state"] = "closed"
        self.publish()
        self.assertEqual(self.api.issues[0]["state"], "closed")
        self.assertEqual(self.api.comments[1], [])

    def test_cancelled_and_skipped_jobs_create_no_task(self) -> None:
        self.results["coverage_guided"]["result"] = "cancelled"
        self.results["replay_and_mutation"]["result"] = "skipped"
        self.publish()
        self.assertEqual(self.api.issues, [])

    def test_full_comment_window_refuses_without_another_write(self) -> None:
        self.results["coverage_guided"]["result"] = "failure"
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
        self.results["coverage_guided"]["result"] = "failure"
        self.publish()
        self.api.issues.append(dict(self.api.issues[0]))
        writes = sum(method == "POST" for method, _ in self.api.requests)
        with self.assertRaisesRegex(AssertionError, "Duplicate Probe"):
            self.publish()
        self.assertEqual(sum(method == "POST" for method, _ in self.api.requests), writes)

    def test_surviving_mutants_request_review_without_a_vulnerability_claim(self) -> None:
        self.results["replay_and_mutation"]["outputs"]["review_count"] = "2"
        self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertIn("**review**", self.api.issues[0]["body"])
        self.assertIn("not a vulnerability claim", self.api.issues[0]["body"])
        self.results["replay_and_mutation"]["outputs"]["review_count"] = "0"
        self.publish()
        self.publish()
        self.assertEqual(len(self.api.comments[1]), 1)


if __name__ == "__main__":
    unittest.main()
