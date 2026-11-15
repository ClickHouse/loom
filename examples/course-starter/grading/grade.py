"""Checks the labs against a running Loom stack.

Run `python grading/grade.py` from the repository's top directory, with the
stack running. It runs the lab code, reads back what it wrote, prints one line
per check and exits non-zero when any check fails. In GitHub Actions it also
writes the table to the job summary.

Lab 3 and the written answers in answers.md are graded by hand.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType

from loom import Agent

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("LOOM_BASE_URL", "http://localhost:7777")
INSTALL_DIR = os.environ.get("LOOM_INSTALL_DIR", str(Path.home() / ".loom"))
ABSTAIN_CODES = {
    "no_match",
    "below_threshold",
    "entity_absent",
    "low_calibrated_confidence",
    "page_expired",
}


class Failed(Exception):
    """A check's reason for failing, shown to the student."""


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "labs" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_student(step: Awaitable[object]) -> object:
    try:
        return await step
    except NotImplementedError as e:
        raise Failed(f"{e} is not written yet") from e


async def lab1_fact_saved_and_revised() -> None:
    lab1 = load("lab1")
    async with lab1.make_agent() as agent:
        before = await agent.memory.history(lab1.KEY, limit=1)
        head = before[0]["version"] if before else 0
        await run_student(lab1.save_and_revise(agent))
        got = await agent.memory.get(lab1.KEY)
        # Versions grow with time, so the ones written by this run are above the old head.
        written = [v for v in await agent.memory.history(lab1.KEY) if v["version"] > head]
    if not got.get("found"):
        raise Failed(f"no fact under {lab1.KEY}")
    if len(written) < 2:
        raise Failed(f"save_and_revise wrote {len(written)} version(s) of {lab1.KEY}, not 2")


async def lab1_abstain_code() -> None:
    lab1 = load("lab1")
    async with lab1.make_agent() as agent:
        code = await run_student(lab1.unknown_question(agent))
    if code not in ABSTAIN_CODES:
        raise Failed(f"unknown_question returned {code!r}, not an abstain code")


async def lab2_cited_hit() -> None:
    lab2 = load("lab2")
    if not lab2.QUESTION or not lab2.UNANSWERABLE:
        raise Failed("set QUESTION and UNANSWERABLE")
    async with lab2.make_agent() as agent:
        await run_student(lab2.ingest(agent))
        hits = await agent.knowledge.search(lab2.QUESTION, top_k=1)
    if not hits or hits[0].source_uri != lab2.SOURCE_URI:
        raise Failed(f"the best hit for QUESTION is not from {lab2.SOURCE_URI}")
    if not hits[0].section:
        raise Failed("the best hit names no section of notes.md; give it ## headings")


async def lab2_cutoff() -> None:
    lab2 = load("lab2")
    async with lab2.make_agent() as agent:
        if not await run_student(lab2.answerable(agent, lab2.QUESTION)):
            raise Failed("answerable(QUESTION) is False")
        if await run_student(lab2.answerable(agent, lab2.UNANSWERABLE)):
            raise Failed("answerable(UNANSWERABLE) is True")


def compose(*args: str, stdin: str = "") -> str:
    done = subprocess.run(
        ["docker", "compose", "--project-directory", INSTALL_DIR, *args],
        input=stdin,
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise Failed(done.stderr.strip() or done.stdout.strip())
    return done.stdout


async def lab4_query() -> None:
    # A fact of the grader's own, so the query has a row to find whatever the
    # other labs wrote.
    grader = Agent(org="dev", namespace="local", agent="course-grader", consumer="course-grading")
    async with grader:
        await grader.memory.set("grading/check/lab4", "The grader ran the Lab 4 check.")
    # The grader's own read-only user, so the student's password stays as it is.
    granted = compose(
        "exec", "-T", "loom", "loom", "sql-access", "grant",
        "--user", "course_grader", "--org", "dev", "--namespaces", "local", "--rotate",
    )  # fmt: skip
    password = granted.split("password:")[-1].strip()
    query = (ROOT / "labs" / "lab4.sql").read_text()
    out = compose(
        "exec", "-T", "clickhouse", "clickhouse-client",
        "--user", "course_grader", "--password", password, "--format", "JSONEachRow",
        stdin=query,
    )  # fmt: skip
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    if not rows:
        raise Failed("lab4.sql returns no rows")
    if not any(row.get("agent") == "course-grader" for row in rows):
        raise Failed("no row has agent = 'course-grader', whose fact was saved a moment ago")


CHECKS: list[tuple[str, Callable[[], Awaitable[None]]]] = [
    ("Lab 1: a fact is saved and revised", lab1_fact_saved_and_revised),
    ("Lab 1: a search nothing answers abstains", lab1_abstain_code),
    ("Lab 2: the question finds a cited passage", lab2_cited_hit),
    ("Lab 2: the cutoff separates the questions", lab2_cutoff),
    ("Lab 4: the query counts facts by agent", lab4_query),
]


def stack_answers() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/v1/health", timeout=10) as reply:
            return reply.status == 200
    except OSError:
        return False


async def run_checks() -> list[tuple[str, bool, str]]:
    results = []
    for title, check in CHECKS:
        try:
            await check()
            results.append((title, True, ""))
        except Failed as e:
            results.append((title, False, str(e)))
        except Exception as e:  # noqa: BLE001 — a student's bug fails its own check only
            results.append((title, False, f"{type(e).__name__}: {e}"))
    return results


def main() -> int:
    if not stack_answers():
        print(f"Loom does not answer at {BASE_URL}; start the stack first (README.md).")
        return 2
    results = asyncio.run(run_checks())
    for title, ok, why in results:
        print(f"{'PASS' if ok else 'FAIL'}  {title}" + (f": {why}" if why else ""))
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed} of {len(results)} checks pass")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as out:
            out.write(f"### {passed} of {len(results)} checks pass\n\n")
            out.write("| Check | Result |\n| --- | --- |\n")
            for title, ok, why in results:
                out.write(f"| {title} | {'pass' if ok else 'fail: ' + why.replace('|', '/')} |\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
