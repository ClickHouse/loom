"""Exercise trusted SDK analysis-task reporting without network access."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/codeql.yml"
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


class SDKAnalysisPublisher(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _API()
        self.results = {key: {"result": "success"} for key in ("analyze_sdks",)}
        text = _WORKFLOW.read_text().rsplit("          python3 - <<'PY'\n", 1)[1]
        self.source = "\n".join(line[10:] for line in text.splitlines() if line != "          PY")

    def publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "CODEQL_RESULTS": json.dumps(self.results),
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
        self.results["analyze_sdks"]["result"] = "failure"
        self.publish()
        self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.comments[1], [])

    def test_success_and_reappearance_each_append_one_status(self) -> None:
        self.results["analyze_sdks"]["result"] = "failure"
        self.publish()
        self.results["analyze_sdks"]["result"] = "success"
        self.publish()
        self.publish()
        self.results["analyze_sdks"]["result"] = "failure"
        self.publish()
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.comments[1]), 2)

    def test_human_body_replacement_gets_one_recovery_status(self) -> None:
        self.results["analyze_sdks"]["result"] = "failure"
        self.publish()
        self.api.issues[0]["body"] = "Human diagnosis"
        self.publish()
        self.publish()
        self.assertEqual(self.api.issues[0]["body"], "Human diagnosis")
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(len(self.api.comments[1]), 1)

    def test_closed_decision_is_preserved(self) -> None:
        self.results["analyze_sdks"]["result"] = "failure"
        self.publish()
        self.api.issues[0]["state"] = "closed"
        self.publish()
        self.assertEqual(self.api.issues[0]["state"], "closed")
        self.assertEqual(self.api.comments[1], [])

    def test_cancelled_and_skipped_jobs_create_no_task(self) -> None:
        self.results["analyze_sdks"]["result"] = "cancelled"
        self.publish()
        self.results["analyze_sdks"]["result"] = "skipped"
        self.publish()
        self.assertEqual(self.api.issues, [])

    def test_full_comment_window_refuses_without_another_write(self) -> None:
        self.results["analyze_sdks"]["result"] = "failure"
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
        self.results["analyze_sdks"]["result"] = "failure"
        self.publish()
        self.api.issues.append(dict(self.api.issues[0]))
        writes = sum(method == "POST" for method, _ in self.api.requests)
        with self.assertRaisesRegex(AssertionError, "Duplicate SDK static analysis"):
            self.publish()
        self.assertEqual(sum(method == "POST" for method, _ in self.api.requests), writes)


class ExtractedSDKSource(unittest.TestCase):
    def exercise(self, archived: str, source: bytes, *, success: bool) -> None:
        import hashlib
        import zipfile

        block = (
            _WORKFLOW.read_text()
            .split("      - name: Require actual SDK bytes in the extracted source archive\n", 1)[1]
            .split("  maintenance:", 1)[0]
        )
        text = block.split("          python3 - <<'PYCODE'\n", 1)[1]
        script = "\n".join(line[10:] for line in text.splitlines() if line != "          PYCODE")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "checkout"
            name = "sdk/go/client.go"
            original = b"package loom\n"
            expected_name = str(workspace / name).lstrip("/")
            (root / "sdk-codeql-source.json").write_text(
                json.dumps({expected_name: hashlib.sha256(original).hexdigest()})
            )
            archive = root / "sdk-codeql-databases/go/src.zip"
            archive.parent.mkdir(parents=True)
            selected = expected_name if archived == "client" else archived
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(selected, source)
            env = {
                "RUNNER_TEMP": directory,
                "GITHUB_WORKSPACE": str(workspace),
                "SDK_DATABASE": "go",
                "SDK_ANCHOR": name,
                "GITHUB_STEP_SUMMARY": str(root / "summary"),
            }
            with patch.dict(os.environ, env, clear=True):
                if success:
                    exec(compile(script, str(_WORKFLOW), "exec"), {})
                    self.assertIn("1 matching production SDK", (root / "summary").read_text())
                else:
                    with self.assertRaises(AssertionError):
                        exec(compile(script, str(_WORKFLOW), "exec"), {})

    def test_actual_client_bytes_establish_nonempty_source_presence(self) -> None:
        self.exercise("client", b"package loom\n", success=True)

    def test_a_readme_does_not_stand_in_for_extracted_sdk_code(self) -> None:
        self.exercise("README.md", b"documentation", success=False)

    def test_a_client_filename_with_different_bytes_is_rejected(self) -> None:
        self.exercise("client", b"different source", success=False)

    def test_another_project_client_is_not_the_selected_sdk(self) -> None:
        self.exercise("somewhere/sdk/go/client.go", b"package loom\n", success=False)


if __name__ == "__main__":
    unittest.main()
