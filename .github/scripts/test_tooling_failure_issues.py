import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from tooling_failure_issues import BOT, REPO, report, select_workflows, shared_failures


def run(
    number: int,
    *,
    conclusion: str = "failure",
    event: str = "push",
    repository: str = REPO,
    branch: str = "main",
) -> dict:
    return {
        "id": number,
        "created_at": f"2026-01-{number:02d}T00:00:00Z",
        "head_repository": {"full_name": repository},
        "head_branch": branch,
        "event": event,
        "conclusion": conclusion,
        "path": ".github/workflows/external-links.yml",
        "raw_error": "information from the check output",
    }


def fake_api(runs: list[dict], issues: list[dict] | None = None) -> Mock:
    def request(path: str, *, payload: dict | None = None) -> dict | list:
        if payload is not None:
            assert path == "issues"
            assert issues is not None
            issues.append({**payload, "state": "open", "user": {"login": BOT}})
            return {"number": 1}
        if path.startswith("issues?"):
            return issues or []
        if "missing.yml" in path:
            raise HTTPError(path, 404, "Not found", None, None)
        event = parse_qs(urlsplit(path).query)["event"][0]
        return {"workflow_runs": [row for row in runs if row["event"] == event]}

    return Mock(side_effect=request)


class ToolingIssuesTests(unittest.TestCase):
    def test_workflow_run_requires_an_eligible_main_failure(self) -> None:
        event = {"workflow_run": run(2, event="schedule")}
        self.assertEqual(select_workflows("workflow_run", event), ["external-links.yml"])
        for ignored in (
            run(2, repository="fork/loom"),
            run(2, branch="another"),
            run(2, event="pull_request"),
            run(2, conclusion="success"),
            {**run(2), "path": ".github/workflows/unlisted.yml"},
        ):
            self.assertEqual(select_workflows("workflow_run", {"workflow_run": ignored}), [])
        self.assertEqual(select_workflows("pull_request", event), [])

    def test_does_not_create_an_issue_for_an_old_failure(self) -> None:
        api = fake_api([run(2), run(3, event="schedule", conclusion="success")])
        self.assertEqual(report(api, ["external-links.yml"]), 0)
        self.assertFalse(any("payload" in call.kwargs for call in api.call_args_list))

    def test_report_creates_one_issue_and_never_copies_run_content(self) -> None:
        issues: list[dict] = []
        api = fake_api([run(2), run(3, repository="fork/loom")], issues)
        self.assertEqual(report(api, ["external-links.yml"]), 1)
        self.assertEqual(report(api, ["external-links.yml"]), 0)
        self.assertEqual(len(issues), 1)
        body = issues[0]["body"]
        self.assertIn("https://github.com/ClickHouse/loom/actions/runs/2", body)
        self.assertNotIn("information from the check output", body)

    def test_same_run_does_not_recreate_a_closed_decision(self) -> None:
        issues = [
            {
                "body": "<!-- loom-tooling-failure:v1 external-links 2 -->",
                "state": "closed",
                "user": {"login": BOT},
            }
        ]
        api = fake_api([run(2)], issues)
        self.assertEqual(report(api, ["external-links.yml"]), 0)
        self.assertEqual(report(fake_api([run(3)], issues), ["external-links.yml"]), 1)

    def test_the_default_branch_can_be_missing_an_unmerged_workflow(self) -> None:
        api = fake_api([])
        self.assertEqual(report(api, ["missing.yml"]), 0)

    def test_shared_ci_only_reports_targeted_jobs_and_steps(self) -> None:
        jobs = [
            {"name": "pytest integration part 3 of 4", "conclusion": "failure"},
            {"name": "pytest unit part 1", "conclusion": "failure"},
            {"name": "pytest unit part 2", "conclusion": "failure"},
            {"name": "typescript sdk (generated types)", "conclusion": "timed_out"},
            {
                "name": "python sdk wheel (py 3.12)",
                "conclusion": "failure",
                "steps": [
                    {
                        "name": "Report public types from the installed wheel",
                        "conclusion": "skipped",
                    },
                    {
                        "name": "Validate the PyPI description in both distributions",
                        "conclusion": "failure",
                    },
                ],
            },
        ]
        self.assertEqual(
            {key for key, _ in shared_failures("ci.yml", iter(jobs))},
            {"python-unit", "typescript-sdk", "twine"},
        )
