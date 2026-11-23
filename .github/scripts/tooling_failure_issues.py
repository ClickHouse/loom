"""Open maintenance issues for failed tooling checks on the default branch."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = "ClickHouse/loom"
BOT = "github-actions[bot]"
EVENTS = {"push", "schedule", "workflow_dispatch"}
FAILURES = {"failure", "timed_out"}
WORKFLOWS = {
    "browser-smoke.yml": ("dashboard-browsers", "Dashboard browser smoke"),
    "codeql.yml": ("codeql", "CodeQL execution"),
    "codeql-extended.yml": ("codeql-extended", "Extended CodeQL execution"),
    "external-links.yml": ("external-links", "External documentation links"),
    "go-api.yml": ("go-api", "Go SDK API compatibility"),
    "maven-dependency-graph.yml": ("maven-graph", "Maven dependency graph"),
    "rust-advisories.yml": ("rust-advisories", "Rust dependency advisory check"),
    "rust-api.yml": ("rust-api", "Rust SDK API compatibility"),
    "rust-dependency-graph.yml": ("rust-graph", "Rust dependency graph"),
    "scorecard.yml": ("scorecard", "OpenSSF Scorecard"),
    "sdk-native.yml": ("native-sdk", "Native SDK checks"),
    "service-load.yml": ("memory-load", "Synthetic memory load"),
    "typescript-api-report.yml": ("typescript-api", "TypeScript SDK API report"),
    "workflow-lint.yml": ("workflow-lint", "Workflow lint"),
}
SHARED_WORKFLOWS = ("ci.yml", "docker-smoke.yml")
CI_STEPS = {
    "Validate the PyPI description in both distributions": (
        "twine",
        "PyPI description validation",
    ),
    "Report public types from the installed wheel": (
        "pyright",
        "Python SDK type completeness",
    ),
}
MARKER = re.compile(r"<!-- loom-tooling-failure:v1 ([a-z0-9-]+) (\d+) -->")
TITLE = re.compile(r"^\[tooling-ci:([a-z0-9-]+)\]")
Api = Callable[..., Any]


def github_api(path: str, *, payload: dict[str, str] | None = None) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    with urlopen(request, timeout=20) as response:
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("GitHub response exceeded the limit")
    return json.loads(data)


def trusted(run: dict[str, Any]) -> bool:
    return (
        (run.get("head_repository") or {}).get("full_name", "").lower() == REPO.lower()
        and run.get("head_branch") == "main"
        and run.get("event") in EVENTS
    )


def select_workflows(event_name: str, event: dict[str, Any]) -> list[str]:
    workflows = [*WORKFLOWS, *SHARED_WORKFLOWS]
    if event_name in {"schedule", "workflow_dispatch"}:
        return workflows
    if event_name != "workflow_run":
        return []
    run = event.get("workflow_run") or {}
    path = run.get("path", "").split("@", 1)[0]
    workflow = path.removeprefix(".github/workflows/")
    if trusted(run) and run.get("conclusion") in FAILURES and workflow in workflows:
        return [workflow]
    return []


def latest_run(api: Api, workflow: str) -> dict[str, Any] | None:
    endpoint = f"actions/workflows/{workflow}/runs"
    query = urlencode({"branch": "main", "status": "completed", "per_page": 5})
    # Ask for each trusted event directly so PR runs cannot crowd out a scheduled run.
    matches: list[dict[str, Any]] = []
    for event in EVENTS:
        try:
            page = api(f"{endpoint}?{query}&event={event}")
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        matches.extend(run for run in page["workflow_runs"] if trusted(run))
    # Compare original run dates so rerunning older commits does not revive stale failures.
    return max(matches, key=lambda run: (run["created_at"], run["id"]), default=None)


def list_rows(api: Api, path: str, *, field: str | None = None) -> Iterator[dict[str, Any]]:
    separator = "&" if "?" in path else "?"
    page = 1
    while True:
        result = api(f"{path}{separator}per_page=100&page={page}")
        rows = result[field] if field else result
        yield from rows
        if len(rows) < 100:
            break
        page += 1


def shared_failures(workflow: str, jobs: Iterator[dict[str, Any]]) -> set[tuple[str, str]]:
    failures: set[tuple[str, str]] = set()
    for job in jobs:
        if job.get("conclusion") not in FAILURES:
            continue
        name = job["name"]
        if workflow == "docker-smoke.yml" and name in {
            "Quickstart (amd64)",
            "Quickstart (arm64)",
        }:
            failures.add(("docker-quickstart", "Packaged quickstart"))
        if workflow != "ci.yml":
            continue
        if name.startswith("typescript sdk ("):
            failures.add(("typescript-sdk", "TypeScript SDK checks"))
        if name.startswith("pytest unit part "):
            failures.add(("python-unit", "Python unit checks"))
        if name == "python sdk wheel (py 3.12)":
            for step in job["steps"]:
                if step["name"] in CI_STEPS and step.get("conclusion") in FAILURES:
                    failures.add(CI_STEPS[step["name"]])
    return failures


def collect_failures(api: Api, workflows: list[str]) -> dict[str, tuple[str, int]]:
    failures = {}
    for workflow in workflows:
        run = latest_run(api, workflow)
        if run is None or run.get("conclusion") not in FAILURES:
            continue
        run_id = run["id"]
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("Expected a positive workflow run ID")
        if workflow in WORKFLOWS:
            checks = {WORKFLOWS[workflow]}
        else:
            # The run jobs endpoint includes the latest result of every job, including
            # successful jobs retained when GitHub reruns only the failures.
            checks = shared_failures(
                workflow,
                list_rows(api, f"actions/runs/{run_id}/jobs?filter=latest", field="jobs"),
            )
        for key, label in checks:
            failures[key] = (label, run_id)
    return failures


def existing_issues(api: Api, failures: dict[str, tuple[str, int]]) -> set[str]:
    matched = set()
    query = urlencode({"state": "all", "creator": BOT})
    for issue in list_rows(api, f"issues?{query}"):
        if "pull_request" in issue or (issue.get("user") or {}).get("login") != BOT:
            continue
        body = issue.get("body") or ""
        for key, run_id in MARKER.findall(body):
            if key in failures and (issue["state"] == "open" or int(run_id) == failures[key][1]):
                matched.add(key)
        title = TITLE.match(issue.get("title") or "")
        if title and issue["state"] == "open":
            matched.add(title[1])
    return matched


def report(api: Api, workflows: list[str]) -> int:
    failures = collect_failures(api, workflows)
    if not failures:
        return 0
    matched = existing_issues(api, failures)
    created = 0
    for key in sorted(failures.keys() - matched):
        label, run_id = failures[key]
        api(
            "issues",
            payload={
                "title": f"[tooling-ci:{key}] {label} failed on main",
                "body": (
                    f"<!-- loom-tooling-failure:v1 {key} {run_id} -->\n\n"
                    "### Company or project name\n\n"
                    "ClickHouse\n\n"
                    f"{label} reported a workflow failure on `main`.\n\n"
                    f"[Review the checks]("
                    f"https://github.com/{REPO}/actions/runs/{run_id})\n"
                ),
            },
        )
        created += 1
    return created


def main() -> None:
    if os.environ["GITHUB_REPOSITORY"] != REPO or os.environ["GITHUB_REF"] != "refs/heads/main":
        raise SystemExit("Tooling issues require the default branch of the owning repository")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    workflows = select_workflows(os.environ["GITHUB_EVENT_NAME"], event)
    print(f"Tooling maintenance issues opened: {report(github_api, workflows)}")


if __name__ == "__main__":
    main()
