"""Publish non-sensitive workflow maintenance findings from trusted hosted runs."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

_REPO = "ClickHouse/loom"
_ROOT = "/repos/" + _REPO + "/issues"
_BOT = "github-actions[bot]"
# These rules describe maintainability, not vulnerability or exploit claims.
_PUBLIC_RULES = {
    "anonymous-definition": "Name the workflow jobs and steps so audit locations are clear.",
    "superfluous-actions": "Review the action dependency and use a runner tool when equivalent.",
    "undocumented-permissions": "Document why each explicit workflow permission is needed.",
}
_MARKER = re.compile(r"<!-- loom-workflow-audit:v1 ([a-f0-9]{64}) -->")
_STATE = re.compile(r"<!-- loom-workflow-state:v1 ([a-f0-9]{64}) -->")
_TITLE = re.compile(r"\[workflow-audit:([a-f0-9]{16})\]$")


def fingerprint(rule: str, path: str) -> str:
    return hashlib.sha256(("zizmor\0" + rule + "\0" + path).encode()).hexdigest()


def public_findings(findings: object) -> dict[str, dict[str, Any]]:
    if not isinstance(findings, list) or len(findings) > 1000:
        raise ValueError("Expected at most 1000 workflow findings")
    groups: dict[str, dict[str, Any]] = {}
    for finding in findings:
        rule = finding["ident"]
        if rule not in _PUBLIC_RULES:
            continue  # Complete security findings stay in the hosted audit and Security tab.
        primary = [item for item in finding["locations"] if item["symbolic"]["kind"] == "Primary"]
        if len(primary) != 1:
            raise ValueError("Expected exactly one primary finding location")
        path = primary[0]["symbolic"]["key"]["Local"]["verbatim_path"].removeprefix("./")
        parts = PurePosixPath(path).parts
        if (
            len(path) > 240
            or not re.fullmatch(r"\.github/[A-Za-z0-9_./-]+\.ya?ml", path)
            or ".." in parts
        ):
            raise ValueError("Invalid workflow finding path")
        severity = finding["determinations"]["severity"]
        confidence = finding["determinations"]["confidence"]
        if severity not in {"Informational", "Low", "Medium", "High", "Critical", "Unknown"}:
            raise ValueError("Unknown finding severity")
        if confidence not in {"Low", "Medium", "High", "Unknown"}:
            raise ValueError("Unknown finding confidence")
        # A newly severe determination is routed to security review, never a public issue.
        if severity not in {"Informational", "Low"}:
            continue
        key = fingerprint(rule, path)
        group = groups.setdefault(key, {"rule": rule, "path": path, "observations": []})
        group["observations"].append((severity, confidence))
    for group in groups.values():
        group["observations"].sort()
    return groups


def state_hash(group: dict[str, Any] | None) -> str:
    # Exclude commit/run/line positions, so a new run or shifted source cannot spam updates.
    return hashlib.sha256(json.dumps(group, sort_keys=True).encode()).hexdigest()


def status_body(key: str, group: dict[str, Any] | None, run_url: str) -> str:
    state = state_hash(group)
    if group is None:
        message = "This maintenance finding is absent from the current eligible audit results."
    else:
        message = (
            f"The current audit reports {len(group['observations'])} maintenance observation(s) "
            f"for `{group['rule']}` in `{group['path']}`.\n\n" + _PUBLIC_RULES[group["rule"]]
        )
    return (
        f"<!-- loom-workflow-audit:v1 {key} -->\n"
        f"<!-- loom-workflow-state:v1 {state} -->\n\n{message}\n\n"
        "This is an automated maintenance task, not a confirmed vulnerability. "
        "Review the source before changing behavior. Security-sensitive findings follow "
        "[SECURITY.md](https://github.com/ClickHouse/loom/blob/main/SECURITY.md).\n\n"
        f"[Hosted audit result]({run_url}). "
        "Automation preserves issue bodies, titles and closed decisions."
    )


class GitHub:
    """Fixed-host client with bounded calls, responses and wall time; no redirects or retries."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.calls = 0
        self.deadline = time.monotonic() + 180

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self.calls >= 40 or time.monotonic() >= self.deadline:
            raise RuntimeError("Workflow audit API budget exhausted")
        if not (
            (path.startswith(_ROOT) and method in {"GET", "POST"})
            or (path == "/graphql" and method == "POST")
        ):
            raise ValueError("Unapproved audit API request")
        self.calls += 1
        connection = http.client.HTTPSConnection("api.github.com", timeout=10)
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "loom-workflow-audit",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        body = None if payload is None else json.dumps(payload).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(2 * 1024 * 1024 + 1)
            if response.status != (200 if method == "GET" or path == "/graphql" else 201):
                raise RuntimeError(f"Workflow audit API returned HTTP {response.status}; no retry")
            if len(data) > 2 * 1024 * 1024:
                raise RuntimeError("Workflow audit API response exceeds budget")
            return json.loads(data)
        finally:
            connection.close()

    def issue_snapshot(self) -> list[dict[str, Any]]:
        # Fetch the latest comment with each issue, rather than one read per unchanged task.
        query = """query($cursor:String) { repository(owner:"ClickHouse",name:"loom") {
          issues(first:100,after:$cursor,filterBy:{createdBy:"github-actions[bot]"},
                 states:[OPEN,CLOSED],orderBy:{field:CREATED_AT,direction:ASC}) {
            nodes { number title body state author {login}
                    comments:timelineItems(last:100,itemTypes:[ISSUE_COMMENT]) {
                      totalCount nodes { ... on IssueComment {
                        fullDatabaseId createdAt body author {login}
                      } }
                    } }
            pageInfo {hasNextPage endCursor}
          }
        } }"""
        output: list[dict[str, Any]] = []
        cursor = None
        seen: set[str] = set()
        for _ in range(10):
            response = self.request(
                "POST", "/graphql", {"query": query, "variables": {"cursor": cursor}}
            )
            if response.get("errors"):
                raise RuntimeError("Workflow issue snapshot query failed; no fallback")
            page = response["data"]["repository"]["issues"]
            nodes, info = page["nodes"], page["pageInfo"]
            if not isinstance(nodes, list) or len(nodes) > 100:
                raise ValueError("Invalid workflow issue snapshot page")
            for issue in nodes:
                comments = issue["comments"]
                latest = comments["nodes"]
                if not isinstance(latest, list) or len(latest) > 100:
                    raise ValueError("Invalid workflow issue comment window")
                output.append(
                    {
                        "number": issue["number"],
                        "title": issue["title"],
                        "body": issue["body"],
                        "state": issue["state"].lower(),
                        "user": issue["author"] or {},
                        "comment_count": comments["totalCount"],
                        "recent_comments": [
                            {
                                "id": int(comment["fullDatabaseId"]),
                                "created_at": comment["createdAt"],
                                "body": comment["body"],
                                "user": comment["author"] or {},
                            }
                            for comment in latest
                        ],
                    }
                )
            if info["hasNextPage"] is False:
                return output
            cursor = info["endCursor"]
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("Invalid workflow issue snapshot cursor")
            seen.add(cursor)
        raise RuntimeError("Workflow issue snapshot pagination budget exhausted")


def managed_issues(
    issues: list[Any], groups: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    known = {key[:16]: key for key in groups}
    output = {}
    for issue in issues:
        if "pull_request" in issue or issue.get("user", {}).get("login") != _BOT:
            continue
        matches = _MARKER.findall(issue.get("body") or "")
        title = _TITLE.search(issue.get("title") or "")
        if not matches and title and title[1] in known:
            matches = [known[title[1]]]  # A human can replace the body; never overwrite it.
        if not matches:
            continue
        if len(matches) != 1 or matches[0] in output:
            raise ValueError("Ambiguous managed workflow issue; refusing duplicate publication")
        if type(issue.get("number")) is not int or issue["number"] <= 0:
            raise ValueError("Invalid managed workflow issue number")
        if issue.get("state") not in {"open", "closed"}:
            raise ValueError("Invalid managed workflow issue state")
        output[matches[0]] = issue
    return output


def publish(api: Any, groups: dict[str, dict[str, Any]], run_url: str) -> dict[str, int]:
    issues = api.issue_snapshot()
    managed = managed_issues(issues, groups)
    counts = {"created": 0, "updated": 0, "unchanged": 0, "closed_preserved": 0, "deferred": 0}
    for key in sorted(set(groups) | set(managed)):
        group, issue = groups.get(key), managed.get(key)
        if issue is not None and issue["state"] == "closed":
            counts["closed_preserved"] += 1
            continue
        if counts["created"] + counts["updated"] >= 10:
            counts["deferred"] += 1
            continue
        if issue is None:
            title = f"Review {group['rule']} in {group['path']} [workflow-audit:{key[:16]}]"
            if len(title) > 250:
                raise ValueError("Workflow issue title exceeds budget")
            api.request("POST", _ROOT, {"title": title, "body": status_body(key, group, run_url)})
            counts["created"] += 1
            continue
        number = issue["number"]
        states = _STATE.findall(issue.get("body") or "")
        comments = issue.get("recent_comments", [])
        recent_states = []
        # Status authority follows creation, not human edits to an older bot comment.
        for comment in sorted(
            comments, key=lambda item: (datetime.fromisoformat(item["created_at"]), item["id"])
        ):
            if comment.get("user", {}).get("login") == _BOT:
                recent_states.extend(_STATE.findall(comment.get("body") or ""))
        if issue.get("comment_count", 0) > len(comments) and not recent_states:
            raise RuntimeError("Latest automated state is outside the 100-comment history budget")
        states.extend(recent_states)
        if states and state_hash(group) == states[-1]:
            counts["unchanged"] += 1
            continue
        current = api.request("GET", f"{_ROOT}/{number}")
        if current.get("state") == "closed":
            counts["closed_preserved"] += 1
            continue
        # Append a deduplicated status update; never PATCH a human-owned title or body.
        api.request(
            "POST", f"{_ROOT}/{number}/comments", {"body": status_body(key, group, run_url)}
        )
        counts["updated"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("findings", type=Path)
    args = parser.parse_args()
    if not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == _REPO
        and os.environ.get("GITHUB_EVENT_NAME") in {"push", "schedule"}
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    ):
        raise RuntimeError("Issue publication requires a trusted default-branch hosted run")
    run = os.environ["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run):
        raise ValueError("Invalid hosted run identifier")
    if args.findings.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Workflow findings exceed input budget")
    groups = public_findings(json.loads(args.findings.read_text()))
    counts = publish(
        GitHub(os.environ["GH_TOKEN"]), groups, f"https://github.com/{_REPO}/actions/runs/{run}"
    )
    summary = "Workflow maintenance issues: " + json.dumps(counts, sort_keys=True)
    print(summary)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write("## Automatic workflow maintenance issues\n\n" + summary + "\n")


if __name__ == "__main__":
    main()
