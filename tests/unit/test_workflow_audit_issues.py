"""Exercise automatic audit issue publication with an in-memory API."""

from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "workflow_audit_issues", ROOT / ".github/scripts/workflow_audit_issues.py"
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
URL = "https://github.com/ClickHouse/loom/actions/runs/123"


def finding(
    rule="undocumented-permissions", path="./.github/workflows/example.yml", severity="Low", row=2
):
    return {
        "ident": rule,
        "desc": "DO NOT COPY SOURCE OR SECRET",
        "determinations": {"severity": severity, "confidence": "High"},
        "locations": [
            {
                "symbolic": {"kind": "Primary", "key": {"Local": {"verbatim_path": path}}},
                "concrete": {"location": {"start_point": {"row": row}}},
            }
        ],
    }


class FakeGitHub:
    def __init__(self):
        self.issues, self.comments, self.calls = [], {}, []
        self.fail_write = False

    def pages(self, path, **kwargs):
        self.calls.append(("LIST", path, kwargs))
        if path.endswith("/comments"):
            return copy.deepcopy(self.comments.get(int(path.split("/")[-2]), []))
        return copy.deepcopy(self.issues)

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            return copy.deepcopy(
                next(issue for issue in self.issues if issue["number"] == int(path.split("/")[-1]))
            )
        if self.fail_write:
            raise RuntimeError("Synthetic API refusal")
        if path.endswith("/comments"):
            number = int(path.split("/")[-2])
            self.comments.setdefault(number, []).append(
                {
                    "id": len(self.comments.get(number, [])) + 1,
                    "user": {"login": AUDIT._BOT},
                    **payload,
                }
            )
            return {"id": len(self.comments[number])}
        issue = {
            "number": len(self.issues) + 1,
            "state": "open",
            "user": {"login": AUDIT._BOT},
            **payload,
        }
        self.issues.append(issue)
        return issue


class WorkflowAuditIssues(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.groups = AUDIT.public_findings([finding()])

    def test_creates_actionable_issue_without_raw_source(self):
        self.assertEqual(AUDIT.publish(self.api, self.groups, URL)["created"], 1)
        body = self.api.issues[0]["body"]
        self.assertIn("Document why", body)
        self.assertNotIn("DO NOT COPY", body)
        self.assertIn("not a confirmed vulnerability", body)

    def test_new_run_and_shifted_lines_do_not_create_or_comment(self):
        AUDIT.publish(self.api, self.groups, URL)
        changed = AUDIT.public_findings([finding(row=900)])
        self.assertEqual(AUDIT.publish(self.api, changed, URL + "4")["unchanged"], 1)
        self.assertEqual(len(self.api.issues), 1)
        self.assertFalse(self.api.comments)

    def test_changed_count_adds_one_deduplicated_update(self):
        AUDIT.publish(self.api, self.groups, URL)
        changed = AUDIT.public_findings([finding(), finding(row=20)])
        self.assertEqual(AUDIT.publish(self.api, changed, URL)["updated"], 1)
        self.assertEqual(AUDIT.publish(self.api, changed, URL)["unchanged"], 1)
        self.assertEqual(len(self.api.comments[1]), 1)

    def test_human_title_and_body_are_never_overwritten(self):
        AUDIT.publish(self.api, self.groups, URL)
        issue = self.api.issues[0]
        marker = AUDIT._MARKER.search(issue["body"])[0]
        issue.update(title="Human title", body=marker + "\nHuman notes")
        before = copy.deepcopy(issue)
        AUDIT.publish(self.api, AUDIT.public_findings([finding(), finding(row=20)]), URL)
        self.assertEqual(issue, before)
        self.assertFalse(any(call[0] in {"PATCH", "PUT", "DELETE"} for call in self.api.calls))

    def test_replaced_body_retains_title_identity(self):
        AUDIT.publish(self.api, self.groups, URL)
        self.api.issues[0]["body"] = "Human replaced the entire body"
        AUDIT.publish(self.api, self.groups, URL)
        self.assertEqual(len(self.api.issues), 1)
        self.assertEqual(self.api.issues[0]["body"], "Human replaced the entire body")

    def test_closed_decision_is_never_reopened_or_commented(self):
        AUDIT.publish(self.api, self.groups, URL)
        self.api.issues[0]["state"] = "closed"
        self.api.calls.clear()
        result = AUDIT.publish(self.api, AUDIT.public_findings([finding(), finding(row=5)]), URL)
        self.assertEqual(result["closed_preserved"], 1)
        self.assertFalse(any(call[0] == "POST" for call in self.api.calls))

    def test_absent_finding_updates_once_without_closing(self):
        AUDIT.publish(self.api, self.groups, URL)
        self.assertEqual(AUDIT.publish(self.api, {}, URL)["updated"], 1)
        self.assertEqual(AUDIT.publish(self.api, {}, URL)["unchanged"], 1)
        self.assertEqual(self.api.issues[0]["state"], "open")

    def test_reappearing_finding_updates_the_latest_resolved_status(self):
        AUDIT.publish(self.api, self.groups, URL)
        AUDIT.publish(self.api, {}, URL)
        self.assertEqual(AUDIT.publish(self.api, self.groups, URL)["updated"], 1)
        self.assertEqual(len(self.api.comments[1]), 2)

    def test_sensitive_and_unknown_rules_never_become_public_issues(self):
        rows = [
            finding("template-injection", severity="High"),
            finding("cache-poisoning"),
            finding(severity="High"),
            finding("future-audit"),
        ]
        self.assertEqual(AUDIT.public_findings(rows), {})
        self.assertEqual(AUDIT.publish(self.api, {}, URL)["created"], 0)

    def test_rule_and_path_group_stable_identities(self):
        groups = AUDIT.public_findings(
            [finding(), finding(row=10), finding("anonymous-definition")]
        )
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(len(group["observations"]) for group in groups.values()), [1, 2])
        self.assertEqual(
            set(groups),
            {AUDIT.fingerprint(group["rule"], group["path"]) for group in groups.values()},
        )

    def test_invalid_paths_and_report_shape_fail_before_api(self):
        for value in (
            None,
            [finding(path="../secret.yml")],
            [finding(path=".github/workflows/../../secret.yml")],
            [finding(severity="invalid")],
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    AUDIT.public_findings(value)
        self.assertFalse(self.api.calls)

    def test_ambiguous_existing_identity_stops_before_write(self):
        AUDIT.publish(self.api, self.groups, URL)
        self.api.issues.append(copy.deepcopy(self.api.issues[0]))
        self.api.calls.clear()
        with self.assertRaises(ValueError):
            AUDIT.publish(self.api, self.groups, URL)
        self.assertFalse(any(call[0] == "POST" for call in self.api.calls))

    def test_api_refusal_propagates_without_retry(self):
        self.api.fail_write = True
        with self.assertRaises(RuntimeError):
            AUDIT.publish(self.api, self.groups, URL)
        self.assertEqual(sum(call[0] == "POST" for call in self.api.calls), 1)

    def test_privileged_jobs_require_trusted_main_and_scan_their_own_checkout(self):
        workflow = yaml.load(
            (ROOT / ".github/workflows/zizmor.yml").read_text(), Loader=yaml.BaseLoader
        )
        self.assertNotIn("pull_request_target", workflow["on"])
        self.assertTrue(workflow["on"]["schedule"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            workflow["concurrency"]["cancel-in-progress"],
            "${{ github.event_name == 'pull_request' }}",
        )
        for name, permission in (
            ("maintenance_issues", "issues"),
            ("security_alerts", "security-events"),
        ):
            with self.subTest(job=name):
                job = workflow["jobs"][name]
                self.assertEqual(job["permissions"], {"contents": "read", permission: "write"})
                self.assertIn("github.ref == 'refs/heads/main'", job["if"])
                self.assertIn(
                    "github.event_name == 'push' || github.event_name == 'schedule'", job["if"]
                )
                self.assertNotIn("needs", job)
                self.assertEqual(job["timeout-minutes"], "5")
                self.assertTrue(
                    all("download-artifact" not in step.get("uses", "") for step in job["steps"])
                )
                self.assertEqual(job["steps"][0]["with"]["persist-credentials"], "false")

    def test_fork_context_refused_before_input_or_token_access(self):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "ClickHouse/loom",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF": "refs/pull/123/merge",
        }
        with (
            patch.dict(AUDIT.os.environ, env, clear=True),
            patch("sys.argv", ["audit", "/nonexistent-synthetic-report"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "trusted default-branch"):
                AUDIT.main()

    def test_transport_budgets_and_fixed_endpoint_fail_before_network(self):
        client = AUDIT.GitHub("synthetic-token")
        client.calls = 40
        with self.assertRaisesRegex(RuntimeError, "budget"):
            client.request("POST", AUDIT._ROOT, {})
        client.calls = 0
        for method, path in (("PATCH", AUDIT._ROOT), ("GET", "/repos/other/repository/issues")):
            with self.subTest(method=method, path=path):
                with self.assertRaises(ValueError):
                    client.request(method, path)

    def test_write_budget_defers_and_next_run_finishes(self):
        groups = AUDIT.public_findings(
            [finding(path=f".github/workflows/example{i}.yml") for i in range(14)]
        )
        result = AUDIT.publish(self.api, groups, URL)
        self.assertEqual((result["created"], result["deferred"]), (10, 4))
        self.assertEqual(AUDIT.publish(self.api, groups, URL)["created"], 4)
        self.assertEqual(len(self.api.issues), 14)


if __name__ == "__main__":
    unittest.main()
