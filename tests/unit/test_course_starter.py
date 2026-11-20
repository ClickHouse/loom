"""examples/course-starter is copied out of this repository into a teacher's own, so
it reaches Loom's files by URL: those URLs must name files and headings that exist,
and its dev container must stay the root one's, which installs the same stack. Its
grader runs against a scripted Loom, so a changed SDK call fails here rather than in
every course: each check fails on the untouched starter and passes on a solution."""

from __future__ import annotations

import argparse
import asyncio
import functools
import importlib.util
import io
import itertools
import json
import re
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import httpx
import pytest

from server.cli import sql_access
from tests.unit.markdown_links import anchors
from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]
STARTER = ROOT / "examples" / "course-starter"
URL = re.compile(r"https://github\.com/ClickHouse/loom/blob/main/([\w./-]+)(?:#([\w-]+))?")
# Keys the starter sets for itself: its name and its postCreateCommand.
OWN_SETUP = {"name", "postCreateCommand"}
USES = re.compile(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}")


def devcontainer(path: Path) -> dict:
    # Both files hold only whole-line // comments.
    lines = [line for line in path.read_text().splitlines() if not line.strip().startswith("//")]
    return json.loads("\n".join(lines))


def test_devcontainer_matches_the_root_one() -> None:
    root = devcontainer(ROOT / ".devcontainer" / "devcontainer.json")
    starter = devcontainer(STARTER / ".devcontainer" / "devcontainer.json")
    assert {k: v for k, v in starter.items() if k not in OWN_SETUP} == {
        k: v for k, v in root.items() if k not in OWN_SETUP
    }


@pytest.mark.parametrize(
    "name",
    [
        "start-loom.sh",
        "agent-cli/package.json",
        "agent-cli/package-lock.json",
        "agent-cli/bind-native.cjs",
    ],
)
def test_start_script_is_the_root_one(name: str) -> None:
    root = ROOT / ".devcontainer" / name
    assert (STARTER / ".devcontainer" / name).read_bytes() == root.read_bytes()


def test_copied_native_binding_helper_matches_the_installer() -> None:
    helper = ROOT / "scripts" / "code-toolchains" / "bind-native.cjs"
    assert (
        ROOT / ".devcontainer" / "agent-cli" / "bind-native.cjs"
    ).read_bytes() == helper.read_bytes()


def test_workflow_actions_are_pinned_to_commits() -> None:
    """A teacher's copy keeps its pins; a commit, unlike a tag, cannot move under it."""
    workflow = (STARTER / ".github" / "workflows" / "grade.yml").read_text()
    uses = re.findall(r"uses: (\S+)", workflow)
    assert uses and [u for u in uses if not USES.fullmatch(u)] == []


@pytest.mark.parametrize("path", tracked("examples/course-starter"))
def test_loom_urls_resolve(path: str) -> None:
    broken = []
    for match in URL.finditer((ROOT / path).read_text()):
        target, anchor = ROOT / match[1], match[2]
        if not target.is_file():
            broken.append(match[0])
        elif anchor and anchor.lower() not in anchors(target):
            broken.append(match[0])
    assert not broken, broken


# Each lab's TODO block and what a solution writes in its place.
SOLUTION = {
    "labs/lab1.py": [
        (
            """    # TODO: two calls to agent.memory.set(KEY, ...)
    raise NotImplementedError("Lab 1: save_and_revise")""",
            """    await agent.memory.set(KEY, "My favourite database is ClickHouse.")
    await agent.memory.set(KEY, "My favourite database is ClickHouse, for its speed.")""",
        ),
        (
            """    # TODO: agent.memory.search(...), then return the result's abstain_code
    raise NotImplementedError("Lab 1: unknown_question")""",
            """    hits = await agent.memory.search("What is the capital of Mongolia?")
    return hits.abstain_code""",
        ),
    ],
    "labs/lab2.py": [
        ('QUESTION = ""', 'QUESTION = "When are office hours?"'),
        ('UNANSWERABLE = ""', 'UNANSWERABLE = "What is the boiling point of tungsten?"'),
        ("CUTOFF = 1.0", "CUTOFF = 0.65"),
        (
            """    # TODO: agent.knowledge.ingest_file(...)
    raise NotImplementedError("Lab 2: ingest")""",
            "    await agent.knowledge.ingest_file(NOTES, source_uri=SOURCE_URI)",
        ),
        (
            """    # TODO: agent.knowledge.search(...), then compare the hits' similarity with CUTOFF
    raise NotImplementedError("Lab 2: answerable")""",
            """    hits = await agent.knowledge.search(question, top_k=3)
    return bool(hits) and max(h.similarity for h in hits) >= CUTOFF""",
        ),
    ],
    "labs/notes.md": [
        (
            (STARTER / "labs" / "notes.md").read_text(),
            "# Notes\n\n## Office hours\n\nOffice hours are on Tuesdays in room B12.\n",
        )
    ],
    "labs/lab4.sql": [
        (
            "the agent in a column named `agent`.\n",
            "the agent in a column named `agent`.\n"
            "SELECT agent, count() AS facts FROM loom.sql_v1_memories\n"
            "WHERE created_at > now() - INTERVAL 1 HOUR GROUP BY agent\n",
        )
    ],
}


class FakeLoom:
    """The ops the labs and the grader call, over one namespace's memories and
    documents. A document search hit is close when the question shares a word
    with the document."""

    def __init__(self) -> None:
        self.versions: dict[str, list[dict[str, Any]]] = {}  # key -> newest first
        self.agents: dict[str, str] = {}
        self.documents: dict[str, str] = {}
        self.uploads: dict[str, str] = {}
        # Versions only grow, as the server's time stamps do, deletes included.
        self.next_version = itertools.count(1)

    def handle(self, request: httpx.Request) -> httpx.Response:
        op = request.url.path.removeprefix("/v1/")
        if op == "knowledge.upload":
            self.uploads["up-1"] = request.content.decode()
            return httpx.Response(200, json={"upload_id": "up-1"})
        return httpx.Response(200, json=self.answer(op, json.loads(request.content or b"{}")))

    def answer(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        key = body.get("memory_key", "")
        if op == "memory.set":
            version = {"version": next(self.next_version), "value": body["value"]}
            self.versions.setdefault(key, []).insert(0, {**version, "status": "active"})
            self.agents[key] = body["agent"]
            return {"memory_key": key, "status": "active", **version}
        if op == "memory.get":
            live = self.versions.get(key)
            return {"found": True, "value": live[0]["value"]} if live else {"found": False}
        if op == "memory.history":
            return {"rows": self.versions.get(key, [])[: body["limit"]]}
        if op == "memory.delete":
            return {"tombstoned": self.versions.pop(key, None) is not None}
        if op == "memory.search":
            reason = "no row is above the similarity gate"
            return {
                "results": [],
                "abstain": True,
                "abstain_code": "below_threshold",
                "abstain_reason": reason,
            }
        if op == "knowledge.ingest":
            self.documents[body["source_uri"]] = self.uploads[body["upload_id"]]
            return {"ingestion_id": "job-1", "state": "queued"}
        if op == "knowledge.ingest_status":
            return {"found": True, "state": "ready"}
        if op == "knowledge.search":
            return {"results": self.search(body)}
        raise AssertionError(f"the scripted Loom does not answer {op}")

    def search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        words = {w.strip("?").lower() for w in body["query"].split() if len(w) > 3}
        hits = []
        for uri, text in self.documents.items():
            if body.get("uri_prefix") and not uri.startswith(tuple(body["uri_prefix"])):
                continue
            similarity = 0.8 if words & set(text.lower().split()) else 0.4
            hit = {"source_uri": uri, "chunk_idx": 0, "content": text, "tags": []}
            hit |= {"distance": 1 - similarity, "similarity": similarity, "section": "Notes"}
            hits.append(hit)
        return hits[: body["top_k"]]

    def compose(self, *args: str, stdin: str = "") -> str:
        """`docker compose exec` of the grader: the grant prints a password, and
        any query counts the live memories by agent; a file of comments returns nothing."""
        if "sql-access" in args:
            return grant(args[args.index("sql-access") :])
        if not [line for line in stdin.splitlines() if line.strip() and not line.startswith("--")]:
            return ""
        counts: dict[str, int] = {}
        for key in self.versions:
            counts[self.agents[key]] = counts.get(self.agents[key], 0) + 1
        return "".join(json.dumps({"agent": a, "facts": n}) + "\n" for a, n in counts.items())


def grant(argv: tuple[str, ...]) -> str:
    """What the grader's `loom sql-access grant` prints, through the real CLI's
    parser and output with the ClickHouse statements left out."""
    parser = argparse.ArgumentParser()
    sql_access.register_sql_access_cli(parser.add_subparsers())
    args = parser.parse_args(list(argv))
    assert args.rotate, "a grader's user that exists gets a password only with --rotate"
    done = SimpleNamespace(user=args.user, created=False, rows={("dev", "local")}, password="pw")
    with (
        mock.patch.object(sql_access, "_connected", lambda *_, **__: done),
        redirect_stdout(io.StringIO()) as out,
    ):
        assert args.func(args) == 0
    return out.getvalue()


def prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, solved: bool, abstain_answer: str = ""
) -> ModuleType:
    """The grader of a copy of the starter, filled in when `solved`, against a
    scripted Loom; `abstain_answer` replaces what the Lab 1 solution returns
    from its search."""
    copy = tmp_path / "course"
    shutil.copytree(STARTER, copy)
    if solved:
        for path, edits in SOLUTION.items():
            text = (copy / path).read_text()
            for old, new in edits:
                assert old in text, f"{path} no longer holds the block a solution fills in"
                text = text.replace(old, new)
            if abstain_answer and path == "labs/lab1.py":
                text = text.replace("return hits.abstain_code", f"return {abstain_answer}")
            (copy / path).write_text(text)
    loom = FakeLoom()
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(loom.handle)),
    )
    for var in ("LOOM_ORG", "LOOM_NAMESPACE", "LOOM_AGENT", "LOOM_AUTO_RUN", "LOOM_MENTOR_BRIEF"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOOM_API_KEY", "test-token")
    monkeypatch.setenv("LOOM_BASE_URL", "http://loom.test")
    grade = load_grader(copy)
    monkeypatch.setattr(grade, "compose", loom.compose)
    return grade


def grade_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, solved: bool, abstain_answer: str = ""
) -> list[tuple[str, bool, str]]:
    grade = prepare(tmp_path, monkeypatch, solved=solved, abstain_answer=abstain_answer)
    return asyncio.run(grade.run_checks())


def load_grader(copy: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("course_grade", copy / "grading" / "grade.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_check_fails_on_the_untouched_starter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = grade_copy(tmp_path, monkeypatch, solved=False)
    assert [ok for _, ok, _ in results] == [False] * len(results)
    # Each fails for its own reason, not on a grader error.
    assert not [why for _, _, why in results if "Error" in why], results


def test_every_check_passes_on_a_solution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = grade_copy(tmp_path, monkeypatch, solved=True)
    assert [(title, why) for title, ok, why in results if not ok] == []


def test_main_says_when_the_stack_does_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.copytree(STARTER, tmp_path / "course")
    grade = load_grader(tmp_path / "course")
    monkeypatch.setattr(grade, "stack_answers", lambda: False)
    assert grade.main() == 2
    assert "does not answer" in capsys.readouterr().out


def test_main_writes_one_summary_row_per_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(STARTER, tmp_path / "course")
    grade = load_grader(tmp_path / "course")

    async def results() -> list[tuple[str, bool, str]]:
        return [("Lab A", True, ""), ("Lab B", False, "two\nlines | and a pipe")]

    monkeypatch.setattr(grade, "stack_answers", lambda: True)
    monkeypatch.setattr(grade, "run_checks", results)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert grade.main() == 1
    assert summary.read_text().splitlines()[-2:] == [
        "| Lab A | pass |",
        "| Lab B | fail: two lines / and a pipe |",
    ]


@pytest.mark.parametrize("answer", ["hits.abstain_reason", "hits.abstain", "hits"])
def test_a_lab_1_answer_that_is_not_an_abstain_code_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    results = grade_copy(tmp_path, monkeypatch, solved=True, abstain_answer=answer)
    failed = [title for title, ok, _ in results if not ok]
    assert failed == ["Lab 1: a search nothing answers abstains"]


@pytest.mark.parametrize("lab", ["lab1", "lab2"])
def test_a_solved_lab_runs_as_its_docstring_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], lab: str
) -> None:
    """`python labs/<lab>.py`: its main() prints what the lab wrote and found."""
    grade = prepare(tmp_path, monkeypatch, solved=True)
    asyncio.run(grade.load(lab).main())
    printed = capsys.readouterr().out
    assert ("abstain_code: below_threshold" if lab == "lab1" else "answerable: True") in printed


def test_lab_4_rows_are_one_count_per_agent(tmp_path: Path) -> None:
    shutil.copytree(STARTER, tmp_path / "course")
    grade = load_grader(tmp_path / "course")
    assert grade.counts([{"agent": "a", "facts": "2"}, {"agent": "b", "facts": 1}]) == {
        "a": 2,
        "b": 1,
    }
    with pytest.raises(grade.Failed, match="more than one row for agent a"):
        grade.counts([{"agent": "a", "facts": 1}, {"agent": "a", "facts": 2}])
    with pytest.raises(grade.Failed, match="not an agent and one count"):
        grade.counts([{"agent": "a"}])

def test_course_starter_carries_the_repository_license_and_notices() -> None:
    """The starter is copied out as a standalone repository, so Loom's license
    and attribution text must travel with it (and keep no dangling references)."""
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"):
        assert (STARTER / name).is_file(), name
        assert (STARTER / name).read_bytes() == (ROOT / name).read_bytes(), name
