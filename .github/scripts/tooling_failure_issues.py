"""Open maintenance issues for failed checks and scanner alerts on the default branch."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO = "ClickHouse/loom"
BOT = "github-actions[bot]"
EVENTS = {"push", "schedule", "workflow_dispatch"}
FAILURES = {"failure", "timed_out"}
LOOKUP_BUDGET_S = 120
WORKFLOWS = {
    "browser-smoke.yml": ("dashboard-browsers", "Dashboard browser smoke"),
    "codeql.yml": ("codeql", "CodeQL execution"),
    "codeql-extended.yml": ("codeql-extended", "Extended CodeQL execution"),
    "contributor-lab.yml": ("contributor-lab", "Contributor lab"),
    "external-links.yml": ("external-links", "External documentation links"),
    "go-api.yml": ("go-api", "Go SDK API compatibility"),
    "http-contract.yml": ("http-contract", "HTTP schema contract"),
    "identity-testing.yml": ("endpoint-identity", "Endpoint identity probes"),
    "maven-dependency-graph.yml": ("maven-graph", "Maven dependency graph"),
    "rust-advisories.yml": ("rust-advisories", "Rust dependency advisory check"),
    "rust-api.yml": ("rust-api", "Rust SDK API compatibility"),
    "rust-dependency-graph.yml": ("rust-graph", "Rust dependency graph"),
    "scorecard.yml": ("scorecard", "OpenSSF Scorecard"),
    "sdk-native.yml": ("native-sdk", "Native SDK checks"),
    "service-load.yml": ("memory-load", "Synthetic memory load"),
    "typescript-api-report.yml": ("typescript-api", "TypeScript SDK API report"),
    "workflow-lint.yml": ("workflow-lint", "Workflow lint"),
    "zizmor.yml": ("workflow-audit", "Workflow security audit execution"),
}
SHARED_WORKFLOWS = ("ci.yml", "docker-smoke.yml")
ISSUE_PUBLISHERS = {
    "contributor-lab.yml": "Record trusted source-lab failures",
    "http-contract.yml": "Record trusted HTTP contract failures",
    "identity-testing.yml": "Record trusted probe maintenance",
    "sdk-native.yml": "Record trusted SDK failures",
}
SCANNERS = {"CodeQL": "codeql-alerts", "Scorecard": "scorecard-alerts"}
SECURITY_URL = f"https://github.com/{REPO}/security/code-scanning"
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


@dataclass(frozen=True)
class Finding:
    source_id: int
    title: str
    description: str
    link_text: str
    link: str


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
    with urlopen(request, timeout=10) as response:
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


def recent_runs(api: Api, workflow: str) -> list[dict[str, Any]]:
    endpoint = f"actions/workflows/{workflow}/runs"
    query = urlencode({"branch": "main", "status": "completed", "per_page": 5})
    # Ask for each trusted event directly so PR runs cannot crowd out a scheduled run.
    matches: list[dict[str, Any]] = []
    for event in EVENTS:
        try:
            page = api(f"{endpoint}?{query}&event={event}")
        except HTTPError as exc:
            if exc.code == 404:
                return []
            raise
        matches.extend(run for run in page["workflow_runs"] if trusted(run))
    # Compare original run dates so rerunning older commits does not revive stale failures.
    return sorted(matches, key=lambda run: (run["created_at"], run["id"]), reverse=True)


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


def shared_results(workflow: str, jobs: Iterator[dict[str, Any]]) -> dict[tuple[str, str], bool]:
    results: dict[tuple[str, str], bool] = {}

    def record(check: tuple[str, str], conclusion: str | None) -> None:
        if conclusion == "success" or conclusion in FAILURES:
            results[check] = results.get(check, False) or conclusion in FAILURES

    for job in jobs:
        name = job["name"]
        conclusion = job.get("conclusion")
        if workflow == "docker-smoke.yml" and name in {
            "Quickstart (amd64)",
            "Quickstart (arm64)",
        }:
            record(("docker-quickstart", "Packaged quickstart"), conclusion)
        if workflow != "ci.yml":
            continue
        if name.startswith("typescript sdk ("):
            record(("typescript-sdk", "TypeScript SDK checks"), conclusion)
        if name.startswith("pytest unit part "):
            record(("python-unit", "Python unit checks"), conclusion)
        if name == "python sdk wheel (py 3.12)":
            for step in job["steps"]:
                if step["name"] in CI_STEPS:
                    record(CI_STEPS[step["name"]], step.get("conclusion"))
    return results


def collect_failures(api: Api, workflows: list[str]) -> dict[str, Finding]:
    failures = {}
    for workflow in workflows:
        runs = recent_runs(api, workflow)
        if workflow in WORKFLOWS:
            runs = runs[:1]
        seen: set[str] = set()
        for run in runs:
            run_id = run["id"]
            if type(run_id) is not int or run_id <= 0:
                raise ValueError("Expected a positive workflow run ID")
            jobs_path = f"actions/runs/{run_id}/jobs?filter=latest"
            if workflow in WORKFLOWS:
                if run.get("conclusion") not in FAILURES:
                    continue
                publisher = ISSUE_PUBLISHERS.get(workflow)
                if publisher and any(
                    job["name"] == publisher and job["conclusion"] == "success"
                    for job in list_rows(api, jobs_path, field="jobs")
                ):
                    continue
                outcomes = {WORKFLOWS[workflow]: True}
            else:
                # A completed shared workflow may omit a tool. Look for a
                # conclusive result for each group before superseding its failure.
                outcomes = shared_results(workflow, list_rows(api, jobs_path, field="jobs"))
            for (key, label), failed in outcomes.items():
                if key in seen:
                    continue
                seen.add(key)
                if failed:
                    failures[key] = Finding(
                        run_id,
                        f"{label} failed on main",
                        f"{label} reported a workflow failure on `main`.",
                        "Review the checks",
                        f"https://github.com/{REPO}/actions/runs/{run_id}",
                    )
            if len(seen) >= (4 if workflow == "ci.yml" else 1):
                break
    return failures


def collect_scanning_findings(api: Api) -> dict[str, Finding]:
    # The issue acknowledges alert numbers. GitHub Security tracks their lifecycle.
    query = urlencode({"state": "open", "ref": "refs/heads/main"})
    latest: dict[str, int] = {}
    for alert in list_rows(api, f"code-scanning/alerts?{query}"):
        tool = (alert.get("tool") or {}).get("name")
        if tool not in SCANNERS or alert.get("state") != "open":
            continue
        number = alert["number"]
        if type(number) is not int or number <= 0:
            raise ValueError("Expected a positive code scanning alert number")
        latest[tool] = max(latest.get(tool, 0), number)
    return {
        SCANNERS[tool]: Finding(
            number,
            f"Review open {tool} alerts on main",
            f"{tool} has open code scanning alerts on `main`.",
            "Review code scanning alerts",
            SECURITY_URL,
        )
        for tool, number in latest.items()
    }


def existing_issues(api: Api, findings: dict[str, Finding]) -> set[str]:
    matched = set()
    query = urlencode({"state": "all", "creator": BOT})
    for issue in list_rows(api, f"issues?{query}"):
        if "pull_request" in issue or (issue.get("user") or {}).get("login") != BOT:
            continue
        body = issue.get("body") or ""
        for key, source_id in MARKER.findall(body):
            if key in findings and (
                issue["state"] == "open" or int(source_id) >= findings[key].source_id
            ):
                matched.add(key)
        title = TITLE.match(issue.get("title") or "")
        if title and issue["state"] == "open":
            matched.add(title[1])
    return matched


def report(api: Api, findings: dict[str, Finding]) -> tuple[int, list[str]]:
    if not findings:
        return 0, []
    matched = existing_issues(api, findings)
    created = 0
    failed: list[str] = []
    for key in sorted(findings.keys() - matched):
        finding = findings[key]
        issue = {
            "title": f"[tooling-ci:{key}] {finding.title}",
            "body": (
                f"<!-- loom-tooling-failure:v1 {key} {finding.source_id} -->\n\n"
                "### Company or project name\n\n"
                "ClickHouse\n\n"
                f"{finding.description}\n\n"
                f"[{finding.link_text}]({finding.link})\n"
            ),
        }
        try:
            api("issues", payload=issue)
        except (OSError, HTTPException, ValueError) as exc:
            failed.append(f"{key} issue creation ({type(exc).__name__})")
        else:
            created += 1
    return created, failed


def main() -> None:
    if os.environ["GITHUB_REPOSITORY"] != REPO or os.environ["GITHUB_REF"] != "refs/heads/main":
        raise SystemExit("Tooling issues require the default branch of the owning repository")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_name = os.environ["GITHUB_EVENT_NAME"]
    # Leave time in the job for reading issue history and publishing the results.
    lookup_deadline = time.monotonic() + LOOKUP_BUDGET_S

    def lookup(path: str) -> Any:
        if time.monotonic() >= lookup_deadline:
            raise TimeoutError("Tooling lookup budget exhausted")
        return github_api(path)

    sources = select_workflows(event_name, event)
    if event_name in {"schedule", "workflow_dispatch"}:
        sources.append("code-scanning")
        # Rotate so a consistently slow prefix cannot starve the last sources.
        offset = int(time.time() // 3600) % len(sources)
        sources = sources[offset:] + sources[:offset]

    findings: dict[str, Finding] = {}
    failed_sources: list[str] = []
    for source in sources:
        try:
            if source == "code-scanning":
                findings.update(collect_scanning_findings(lookup))
            else:
                findings.update(collect_failures(lookup, [source]))
        except (OSError, HTTPException, ValueError, KeyError, TypeError) as exc:
            failed_sources.append(f"{source} ({type(exc).__name__})")
    created, failed_writes = report(github_api, findings)
    failed_sources.extend(failed_writes)
    print(f"Tooling maintenance issues opened: {created}")
    if failed_sources:
        raise SystemExit("Tooling sweep incomplete for " + ", ".join(failed_sources))


if __name__ == "__main__":
    main()
