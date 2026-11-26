from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

_ROOT = Path(__file__).resolve().parents[2]


def _run_lab(track: str, run: Mock) -> tuple[int, list[dict[str, object]]]:
    source = _ROOT / "dev/contributor-lab/check.py"
    spec = importlib.util.spec_from_file_location("contributor_lab", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        def local_path(value: str) -> Path:
            return root if value == "/tmp/loom-contributor-lab" else Path(value)

        with (
            patch.object(module, "Path", side_effect=local_path),
            patch.object(module.os, "chdir"),
            patch.object(module.sys, "argv", [str(source), track]),
            patch.object(module.subprocess, "run", run),
            redirect_stdout(io.StringIO()),
        ):
            status = 0
            try:
                module.main()
            except SystemExit as error:
                status = error.code
        return status, json.loads((root / "report.json").read_text())


def _summary(rows: list[dict[str, object]]) -> str:
    source = _ROOT / ".github/workflows/contributor-lab.yml"
    lines = source.read_text().splitlines()
    step = next(
        i for i, line in enumerate(lines) if line.strip() == "- name: Report actual check counts"
    )
    start = next(i for i in range(step, len(lines)) if lines[i].strip() == "python3 - <<'PY'")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "PY")
    script = "\n" * (start + 1) + textwrap.dedent("\n".join(lines[start + 1 : end]))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        report = root / "loom-source-report.json"
        output = root / "summary.md"
        report.write_text(json.dumps(rows))
        with patch.dict(os.environ, {"RUNNER_TEMP": str(root), "GITHUB_STEP_SUMMARY": str(output)}):
            exec(compile(script, str(source), "exec"), {})
        return output.read_text()


class TestContributorLab(unittest.TestCase):
    def test_timeouts_are_saved_as_failed_results(self) -> None:
        for track in ("functional", "quality"):
            with self.subTest(track=track):
                run = Mock(side_effect=subprocess.TimeoutExpired("synthetic-check", 120))
                status, rows = _run_lab(track, run)
                self.assertEqual(status, 1)
                self.assertEqual(rows, [{"track": track, "passed": False, "error": "timeout"}])
                self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_missing_or_invalid_reports_leave_the_count_unknown(self) -> None:
        for text, error in ((None, "missing report"), ("<testsuites>", "invalid report")):
            with self.subTest(error=error):

                def completed(
                    command: list[str], report_text: str | None = text, **_kwargs: object
                ) -> SimpleNamespace:
                    if report_text is not None:
                        report = next(arg for arg in command if arg.startswith("--junitxml="))
                        Path(report.removeprefix("--junitxml=")).write_text(report_text)
                    return SimpleNamespace(returncode=1)

                status, rows = _run_lab("functional", Mock(side_effect=completed))
                self.assertEqual(status, 1)
                self.assertEqual(rows, [{"track": "functional", "passed": False, "error": error}])

    def test_a_complete_zero_test_report_preserves_its_known_count(self) -> None:
        def completed(command: list[str], **_kwargs: object) -> SimpleNamespace:
            report = next(arg for arg in command if arg.startswith("--junitxml="))
            Path(report.removeprefix("--junitxml=")).write_text(
                '<testsuites><testsuite tests="0" skipped="0" /></testsuites>'
            )
            return SimpleNamespace(returncode=1)

        status, rows = _run_lab("functional", Mock(side_effect=completed))
        self.assertEqual(status, 1)
        self.assertEqual(rows, [{"track": "functional", "tests": 0, "skipped": 0, "passed": False}])

    def test_completed_quality_results_keep_the_exit_status(self) -> None:
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                run = Mock(return_value=SimpleNamespace(returncode=returncode))
                status, rows = _run_lab("quality", run)
                self.assertEqual(status, returncode)
                self.assertEqual(
                    rows, [{"track": "quality", "checks": 2, "passed": returncode == 0}]
                )

    def test_summary_renders_errors_without_inventing_a_count(self) -> None:
        for error in ("timeout", "missing report", "invalid report"):
            with self.subTest(error=error):
                text = _summary([{"track": "functional", "passed": False, "error": error}])
                self.assertIn(f"| functional | unknown | False ({error}) |", text)

    def test_summary_renders_failed_zero_count(self) -> None:
        text = _summary([{"track": "functional", "tests": 0, "passed": False}])
        self.assertIn("| functional | 0 | False |", text)

    def test_summary_preserves_successful_counts(self) -> None:
        text = _summary([{"track": "quality", "checks": 2, "passed": True}])
        self.assertIn("| quality | 2 | True |", text)

    def test_summary_rejects_invalid_or_successful_unknown_counts(self) -> None:
        invalid = [
            {"track": "functional", "tests": 0, "passed": True},
            {"track": "functional", "passed": True, "error": "timeout"},
            {"track": "functional", "passed": False},
            {"track": "functional", "tests": True, "passed": True},
            {"track": "functional", "tests": 100001, "passed": False},
            {"track": "other", "tests": 1, "passed": True},
            {"track": "functional", "tests": 1, "passed": False, "error": "timeout"},
            {"track": "functional", "tests": 1, "passed": "true"},
            {"track": "functional", "passed": False, "error": "diagnostic output"},
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(AssertionError):
                _summary([row])
