"""Collect a source-linked implementation note and hand it to a review agent.

Needs an existing public OSS code index, shared memory namespace and review inbox.
Every SDK call is awaited application code; no model chooses tools or writes code.
Memory writes use the server's configured providers. --help sends no requests.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
from typing import Any

from loom import Agent

IMPLEMENTER = "factory-implementation"
REVIEWER = "factory-reviewer"
TERMINAL = {"done", "failed", "wont_do", "cancelled"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def request(bot: Agent, op: str, **fields: Any) -> dict[str, Any]:
    result = await bot.call_with_meta(op, fields)
    request_id = str(result.request_id or "")
    # Print receipts, never a source body, claim, credential or server error body.
    safe_id = request_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id) else ""
    print(json.dumps({"op": op, "request_id": safe_id, "response_received": True}))
    return dict(result.data)


async def source(bot: Agent, args: argparse.Namespace) -> dict[str, str]:
    fields = {"namespace": args.code_namespace, "brief": True}
    before = await request(bot, "code.index_status", **fields)
    require(before.get("repo") == args.repo, "The code namespace names another repository")
    commit = str(before.get("git_head") or "")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "The index has no full commit")
    require(not before.get("fetch_overdue", True), "The repository fetch is overdue")
    for tier in before.get("compiler_tiers", []):
        require(tier.get("commit") == commit, "A compiler tier is at another commit")
    result = await request(
        bot,
        "code.symbol",
        namespace=args.code_namespace,
        name=args.symbol,
        include_body=True,
        include_history=False,
        include_examples=False,
        include_lessons=False,
        token_budget=4000,
    )
    definitions = result.get("definitions", [])
    require(not result.get("ambiguous") and len(definitions) == 1, "Resolve one definition")
    unit = definitions[0]
    require(unit.get("symbol") == args.symbol, "The definition names another symbol")
    body = unit.get("body")
    require(isinstance(body, str) and bool(body), "The definition has no body")
    require(not unit.get("elided") and unit.get("granularity", "unit") == "unit", "Body elided")
    after = await request(bot, "code.index_status", **fields)
    require(after.get("repo") == args.repo and after.get("git_head") == commit, "Index advanced")
    return {
        "repo": args.repo,
        "commit": commit,
        "symbol": args.symbol,
        "tier": str(unit.get("tier") or "unknown"),
        "path": str(unit["path"]),
        "returned_body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


async def read_note(bot: Agent, key: str, expected: str) -> dict[str, Any]:
    memory = await request(bot, "memory.get", memory_key=key)
    require(memory.get("found") is True, "The implementation note is missing")
    require(memory.get("value") == expected, "The stored note differs from the submitted note")
    require(memory.get("agent") == IMPLEMENTER, "The note names another agent")
    require(memory.get("actor") == "process:" + IMPLEMENTER, "Unexpected writing principal")
    require(memory.get("source_class") == "derived", "Unexpected source classification")
    return memory


async def handoff(
    implementer: Agent,
    reviewer: Agent,
    args: argparse.Namespace,
    key: str,
    note: str,
    baseline: dict[str, str],
    task_ref: dict[str, Any],
) -> str:
    created = await request(
        implementer,
        "task.create",
        inbox=args.review_inbox,
        kind="review",
        title="Recheck a source-linked implementation note",
        brief={"issue_url": args.issue_url, "memory_key": key, "source": baseline},
        idempotency_key=key,
    )
    task = created["task"]
    task_ref["task_id"] = task["task_id"]
    task_ref["fence"] = task["fence"]
    require(task.get("from_subject") == IMPLEMENTER, "Unexpected posting principal")
    require(task.get("status") == "todo", "The review task requires admission")
    claimed = (await request(reviewer, "task.claim", task_id=task_ref["task_id"]))["task"]
    require(claimed.get("owner") == REVIEWER, "The review agent did not claim the task")
    require(claimed.get("last_actor") == "process:" + REVIEWER, "Unexpected review principal")
    require(claimed.get("brief") == task["brief"], "The handoff evidence differs")
    fence = claimed["fence"]
    task_ref["fence"] = fence
    await request(reviewer, "task.heartbeat", task_id=task_ref["task_id"], fence=fence)
    await read_note(reviewer, key, note)
    current = await source(reviewer, args)
    unchanged = all(current[k] == baseline[k] for k in ("path", "symbol", "returned_body_sha256"))
    state = "unchanged_body" if unchanged else "changed_body_needs_review"
    outcome = {
        "source_recheck": state,
        "source": current,
        "memory_key": key,
        "claim_truth": "not_verified",
        "code_review": "not_performed",
        "ci": "not_run",
    }
    await request(
        reviewer, "task.complete", task_id=task_ref["task_id"], fence=fence, outcome=outcome
    )
    finished = await request(implementer, "task.get", task_id=task_ref["task_id"])
    require(finished.get("found") is True, "The completed task is missing")
    require(finished["task"].get("status") == "done", "The handoff did not finish")
    require(
        finished["task"].get("last_actor") == "process:" + REVIEWER,
        "Unexpected completing principal",
    )
    require(finished["task"].get("outcome") == outcome, "The task lost its reported outcome")
    print(
        json.dumps(
            {
                "source_recheck": state,
                "claim_truth": "not_verified",
                "merge_ready": False,
                "source_tier": current["tier"],
            }
        )
    )
    return state


async def cancel_unfinished(implementer: Agent, reviewer: Agent, task_ref: dict[str, Any]) -> None:
    task_id = task_ref["task_id"]
    result = await request(implementer, "task.get", task_id=task_id)
    require(result.get("found") is True, "The created task is missing during cleanup")
    task = result["task"]
    require(task.get("from_subject") == IMPLEMENTER, "Cleanup found another poster")
    if task["status"] in TERMINAL:
        return
    require(
        task.get("fence") == task_ref.get("fence"),
        "The task lease changed; leave it for its operator",
    )
    if task.get("owner") == REVIEWER:
        await request(
            reviewer, "task.cancel", task_id=task_id, fence=task["fence"], reason="Example stopped"
        )
    else:
        require(not task.get("owner"), "Another agent owns the task; leave it for its operator")
        await request(
            implementer,
            "task.cancel",
            task_id=task_id,
            fence=task["fence"],
            reason="Example stopped",
        )


async def workflow(implementer: Agent, reviewer: Agent, args: argparse.Namespace) -> None:
    baseline = await source(implementer, args)
    require(baseline["commit"] == args.commit, "The index is not at the requested commit")
    key = "example/software-factory/" + secrets.token_hex(16)
    source_url = f"https://github.com/{args.repo}/blob/{args.commit}/{baseline['path']}"
    note = (
        f"Operator-supplied review claim about `{args.symbol}`: {args.lesson}\n"
        f"Issue: {args.issue_url}\nSource: {source_url}\n"
        f"Returned body SHA256: {baseline['returned_body_sha256']}\n"
        "This example records the claim and source, without verifying the claim."
    )
    opened = await request(
        implementer, "run.begin", goal="Collect source evidence and hand it to review"
    )
    run_id = opened["run_id"]
    task_ref: dict[str, Any] = {}
    complete_attempted = False
    try:
        written = await request(
            implementer,
            "memory.set",
            memory_key=key,
            value=note,
            run_id=run_id,
            tags=[args.repo_tag],
            source_class="derived",
            source_resource_id=source_url,
            confidence=0.5,
        )
        require(not written.get("noop"), "No new note; never follow or delete a merged peer")
        require(
            bool(written.get("memory_id") and written.get("content_sha256")),
            "Missing write receipt",
        )
        await read_note(implementer, key, note)
        state = await handoff(implementer, reviewer, args, key, note, baseline, task_ref)
        complete_attempted = True
        await request(
            implementer,
            "run.complete",
            run_id=run_id,
            status="completed",
            outcome="none",
            memories_used=[],
            metadata={"source_recheck": state, "code_review": "not_performed", "ci": "not_run"},
        )
    finally:
        try:
            if not complete_attempted:
                await request(
                    implementer, "run.complete", run_id=run_id, status="failed", outcome="none"
                )
        finally:
            try:
                if task_ref:
                    await cancel_unfinished(implementer, reviewer, task_ref)
            finally:
                # Only this invocation's unpredictable key; never a peer or entire scope.
                await request(implementer, "memory.delete", memory_key=key)
                gone = await request(implementer, "memory.get", memory_key=key)
                require(gone.get("found") is False, "The generated note remains readable")
                print(json.dumps({"cleanup": "generated_memory_missing", "history": "retained"}))


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "org",
        "memory-namespace",
        "code-namespace",
        "repo",
        "commit",
        "issue-url",
        "symbol",
        "repo-tag",
        "review-inbox",
        "lesson",
    ):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo)), "Use owner/repo")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", args.commit)), "Use a full commit SHA")
    require(
        bool(
            re.fullmatch(
                r"https://github\.com/" + re.escape(args.repo) + r"/issues/[1-9][0-9]*",
                args.issue_url,
            )
        ),
        "Use a public issue URL for this repository",
    )
    require(
        bool(re.fullmatch(r"repo:[A-Za-z0-9_-]+", args.repo_tag)),
        "Use the repository's recorded tag",
    )
    require(0 < len(args.lesson) <= 1200, "Keep the review claim within 1200 characters")
    return args


async def main(args: argparse.Namespace) -> None:
    implementation_key = os.environ["LOOM_IMPLEMENTATION_API_KEY"]
    review_key = os.environ["LOOM_REVIEW_API_KEY"]
    require(
        bool(implementation_key and review_key) and implementation_key != review_key,
        "Use distinct credentials",
    )
    options: dict[str, Any] = {
        "auto_run": False,
        "mentor_brief": False,
        "coalesce_sets": False,
        "retry": {"max_attempts": 1},
        "pacing": False,
    }
    async with (
        Agent(
            args.org, args.memory_namespace, IMPLEMENTER, api_key=implementation_key, **options
        ) as implementer,
        Agent(args.org, args.memory_namespace, REVIEWER, api_key=review_key, **options) as reviewer,
    ):
        await workflow(implementer, reviewer, args)


if __name__ == "__main__":
    parsed = arguments()
    try:
        asyncio.run(main(parsed))
    except Exception as exc:  # noqa: BLE001 — do not expose server error bodies or credentials
        print(json.dumps({"status": "incomplete", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
