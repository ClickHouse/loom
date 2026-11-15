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
import re
import subprocess
import sys
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType

from loom import Agent

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("LOOM_BASE_URL", "http://localhost:7777")
INSTALL_DIR = os.environ.get("LOOM_INSTALL_DIR", str(Path.home() / ".loom"))


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
    if got is None:
        raise Failed(f"no fact under {lab1.KEY}")
    if len({v["value"] for v in written}) < 2:
        raise Failed(f"save_and_revise did not write {lab1.KEY} with two different values")


async def lab1_abstain_code() -> None:
    lab1 = load("lab1")
    async with lab1.make_agent() as agent:
        code = await run_student(lab1.unknown_question(agent))
    # A code's shape, not a list of codes: a later release may add one, and this
    # copy keeps its checks. A reason, a flag or the hits are not a code.
    if not isinstance(code, str) or not re.fullmatch(r"[a-z]+(_[a-z]+)*", code):
        raise Failed(f"unknown_question returned {code!r}, not an abstain_code")


async def lab2_cited_hit() -> None:
    lab2 = load("lab2")
    if not lab2.QUESTION or not lab2.UNANSWERABLE:
        raise Failed("set QUESTION and UNANSWERABLE")
    async with lab2.make_agent() as agent:
        await run_student(lab2.ingest(agent))
        hits = await agent.knowledge.search(lab2.QUESTION, top_k=1, uri_prefix=[lab2.SOURCE_URI])
    if not hits:
        raise Failed(f"QUESTION finds nothing in {lab2.SOURCE_URI}")
    if not hits[0].section:
        raise Failed("the best hit names no section of notes.md; give it ## headings")


async def lab2_cutoff() -> None:
    lab2 = load("lab2")
    async with lab2.make_agent() as agent:
        # Ingested again, so a cutoff is judged on the notes even when the
        # previous check stopped before them.
        await run_student(lab2.ingest(agent))
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


# What lab4.sql must count, run as the same user.
REFERENCE = """
SELECT agent, count() AS facts FROM loom.sql_v1_memories
WHERE created_at > now() - INTERVAL 1 HOUR GROUP BY agent
"""


def sql(password: str, query: str) -> list[dict[str, object]]:
    out = compose(
        "exec", "-T", "clickhouse", "clickhouse-client",
        "--user", "course_grader", "--password", password, "--format", "JSONEachRow",
        stdin=query,
    )  # fmt: skip
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def counts(rows: list[dict[str, object]]) -> dict[object, int]:
    """Each row's agent and its one count."""
    found = {}
    for row in rows:
        numbers = [int(v) for k, v in row.items() if k != "agent" and str(v).isdigit()]
        if "agent" not in row or len(numbers) != 1:
            raise Failed(f"a row of lab4.sql is not an agent and one count: {row}")
        found[row["agent"]] = numbers[0]
    return found


async def lab4_query() -> None:
    # The grader's own read-only user, so the student's password stays as it is.
    granted = compose(
        "exec", "-T", "loom", "loom", "sql-access", "grant",
        "--user", "course_grader", "--org", "dev", "--namespaces", "local", "--rotate",
    )  # fmt: skip
    if "password:" not in granted:
        raise Failed(f"loom sql-access grant printed no password: {granted.strip()}")
    password = granted.split("password:")[-1].strip()
    # A fact saved now, so the last hour holds a row whatever the other labs
    # wrote; deleted once the check has read it. Its key and value name this
    # run: a value an earlier fact holds is merged into it, and no row is written.
    grader = Agent(org="dev", namespace="local", agent="course-grader", consumer="course-grading")
    run = uuid.uuid4().hex
    key = f"grading/check/lab4/{run}"
    async with grader:
        await grader.memory.set(key, f"The grader ran the Lab 4 check, run {run}.")
        try:
            # Counted before and after: a fact that turns an hour old between the
            # queries leaves the student's count equal to one of the two.
            before = counts(sql(password, REFERENCE))
            got = sql(password, (ROOT / "labs" / "lab4.sql").read_text())
            after = counts(sql(password, REFERENCE))
        finally:
            await grader.memory.delete(key)
    if not got:
        raise Failed("lab4.sql returns no rows")
    if counts(got) not in (before, after):
        raise Failed(f"lab4.sql counts {counts(got)}; the last hour holds {after}")


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
                # A table cell holds one line and no pipe.
                cell = "pass" if ok else "fail: " + why.replace("|", "/").replace("\n", " ")
                out.write(f"| {title} | {cell} |\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
