"""Run the actual label policy against a fake API client."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/help-wanted-label.yml"
FAKE_GH = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_LABEL_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
method = args[args.index("--method") + 1] if "--method" in args else "GET"
path = args[1]
payload = json.loads(sys.stdin.read()) if method == "POST" else None
state["calls"].append({"method": method, "path": path, "payload": payload})
code = state.get("post_exit" if method == "POST" else "get_exit", 0)
if not code and method == "POST":
    for label in payload["labels"]:
        if label not in state["labels"]:
            state["labels"].append(label)
state_path.write_text(json.dumps(state))
if code:
    sys.exit(code)
print(json.dumps(state.get("response", state["labels"])))
"""


class LabelPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.policy = self.workflow["jobs"]["label"]["steps"][0]["run"]

    def run_policy(
        self, labels: list[str], number: str = "1154", **options: object
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object], bool]:
        with tempfile.TemporaryDirectory(prefix="label-policy-") as folder:
            root = Path(folder)
            client = root / "gh"
            client.write_text(FAKE_GH)
            client.chmod(0o755)
            state_path = root / "state.json"
            state_path.write_text(json.dumps({"labels": labels, "calls": [], **options}))
            sentinel = root / "executed"
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                ITEM_NUMBER=number,
                GH_TOKEN="fixture-token",
                GH_HOST="github.com",
                FAKE_LABEL_STATE=str(state_path),
                LABEL_SENTINEL=str(sentinel),
            )
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", self.policy],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            return result, json.loads(state_path.read_text()), sentinel.exists()

    def test_adds_only_discovery_label_and_preserves_others(self) -> None:
        result, state, _ = self.run_policy(["bug", "help wanted", "good first issue"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state["labels"], ["bug", "help wanted", "good first issue", "up-for-grabs"]
        )
        writes = [call for call in state["calls"] if call["method"] != "GET"]
        self.assertEqual(
            writes,
            [
                {
                    "method": "POST",
                    "path": "repos/ClickHouse/loom/issues/1154/labels",
                    "payload": {"labels": ["up-for-grabs"]},
                }
            ],
        )

    def test_current_state_makes_repeated_events_idempotent(self) -> None:
        result, state, _ = self.run_policy(["help wanted", "up-for-grabs"])
        self.assertEqual(result.returncode, 0)
        self.assertTrue(all(call["method"] == "GET" for call in state["calls"]))

    def test_absent_or_removed_help_wanted_does_not_add(self) -> None:
        for labels in ([], ["bug"], ["up-for-grabs"]):
            with self.subTest(labels=labels):
                result, state, _ = self.run_policy(labels)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(state["labels"], labels)
                self.assertTrue(all(call["method"] == "GET" for call in state["calls"]))

    def test_untrusted_label_text_is_data(self) -> None:
        malicious = '$(touch "$LABEL_SENTINEL")\n::error::untrusted'
        result, state, executed = self.run_policy(["help wanted", malicious])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(executed)
        self.assertNotIn("untrusted", result.stdout)
        self.assertEqual(state["labels"], ["help wanted", malicious, "up-for-grabs"])

    def test_invalid_item_numbers_fail_before_api_access(self) -> None:
        for number in ("", "0", "-1", "01", "1/labels", "1; echo invalid", "1" * 11):
            with self.subTest(number=number):
                result, state, _ = self.run_policy(["help wanted"], number)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(state["calls"], [])

    def test_api_read_failure_stops_before_any_write(self) -> None:
        result, state, _ = self.run_policy(["help wanted"], get_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["labels"], ["help wanted"])
        self.assertTrue(all(call["method"] == "GET" for call in state["calls"]))

    def test_malformed_label_response_fails_without_write(self) -> None:
        for response in ({"labels": []}, [None], [42]):
            with self.subTest(response=response):
                result, state, _ = self.run_policy(["help wanted"], response=response)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(all(call["method"] == "GET" for call in state["calls"]))

    def test_api_write_failure_is_not_success_or_a_retry(self) -> None:
        result, state, _ = self.run_policy(["help wanted"], post_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["labels"], ["help wanted"])
        self.assertEqual(sum(call["method"] == "POST" for call in state["calls"]), 1)

    def test_metadata_only_permissions_events_and_coalesced_reconciliation(self) -> None:
        job = self.workflow["jobs"]["label"]
        self.assertEqual(self.workflow["permissions"], {})
        self.assertEqual(job["permissions"], {"issues": "write"})
        self.assertEqual(job["timeout-minutes"], "5")
        self.assertEqual(
            set(self.workflow["on"]), {"issues", "pull_request_target", "workflow_dispatch"}
        )
        self.assertNotIn("synchronize", self.workflow["on"]["pull_request_target"]["types"])
        # The latest unrelated label event can replace a pending help-wanted event.
        # Every queued event must reconcile current state, including our own label.
        self.assertEqual(job["if"], "github.repository == 'ClickHouse/loom'")
        self.assertTrue(all("uses" not in step for step in job["steps"]))
        self.assertEqual(job["steps"][0]["env"]["GH_HOST"], "github.com")


if __name__ == "__main__":
    unittest.main()
