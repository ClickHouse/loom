"""A review brief for a PR the ledger never saw. The merge
commit comes from the mirror's first-parent chain (one bounded grep per PR,
cached), the title from the tracker record or the merge commit, the files from
the merge's diff; the brief says `source: built_from_mirror`, `ledger:
absent`; the on-demand store is the caller's `store` flag and never a
measurement consumer's. A ledger-backed build is byte-identical to the base brief."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

import server.code.app.review_brief as h
from server.code.api.models_review import CodeReviewBriefInput, CodeReviewBriefOutput
from server.code.app.index import review_brief as rb
from server.code.app.index import review_brief_build, review_brief_sources
from server.settings import settings
from tests.unit.test_code_review_brief_sections import (
    DIFF,
    NS,
    ORG,
    PARENT,
    SHA,
    _Calls,
    _fixed_now,
    _Repo,
    _tracker_not_bound,
)

pytestmark = pytest.mark.usefixtures("mirror_here")

MIRROR_SHA = "05c3edcab4349bcf1ff155e32ac74dc321edea0c"


# ---- pure: the merge commit's title -----------------------------------------


def test_merge_title_reads_the_body_for_a_merge_subject_and_the_subject_for_a_squash() -> None:
    assert (
        review_brief_sources.merge_title(
            "Merge pull request #74989 from ClickHouse/fix-ssh", "\nFix SSH exit codes\n\nmore"
        )
        == "Fix SSH exit codes"
    )
    assert (
        review_brief_sources.merge_title("Fix wrong ILIKE results (#117354)", "body")
        == "Fix wrong ILIKE results (#117354)"
    )
    assert (
        review_brief_sources.merge_title("Merge pull request #1 from a/b", "")
        == "Merge pull request #1 from a/b"
    )
    assert review_brief_sources.merge_title("", "") == ""


def test_log_records_parse_the_nul_and_rs_separated_format() -> None:
    text = f"{MIRROR_SHA}\x00Merge pull request #74989 from ClickHouse/fix-ssh\x00Fix SSH\n\x1e\n{SHA}\x00Title (#5)\x00\x1e\n"
    recs = review_brief_sources._parse_log_records(text)
    assert [(s, subj) for s, subj, _ in recs] == [
        (MIRROR_SHA, "Merge pull request #74989 from ClickHouse/fix-ssh"),
        (SHA, "Title (#5)"),
    ]
    assert recs[0][2].startswith("Fix SSH")
    assert (
        review_brief_sources._parse_log_records("") == []
        and review_brief_sources._parse_log_records("garbage\x00x") == []
    )


# ---- the mirror: a real bare repository -------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(cwd),
        "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, text=True, capture_output=True
    ).stdout


@pytest.fixture
def mirror(tmp_path: Path) -> tuple[str, str]:
    """A work tree whose master has two PR merges (#7 with a body title, #77
    squashed) and a merge whose BODY cites `(#7)`, then a bare mirror of it.
    Returns (git_dir, merge sha of #7)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q", "-b", "master")
    (wt / "a.txt").write_text("a\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "init")
    _git(wt, "checkout", "-q", "-b", "fix-7")
    (wt / "src").mkdir()
    (wt / "src" / "f.cpp").write_text("int f() { return 7; }\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "work")
    _git(wt, "checkout", "-q", "master")
    _git(
        wt,
        "merge",
        "-q",
        "--no-ff",
        "fix-7",
        "-m",
        "Merge pull request #7 from org/fix-7\n\nFix the seventh thing\n",
    )
    sha7 = _git(wt, "rev-parse", "HEAD").strip()
    (wt / "b.txt").write_text("b\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "Seventy-seven (#77)")
    _git(wt, "checkout", "-q", "-b", "later")
    (wt / "c.txt").write_text("c\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-q", "-m", "later work")
    _git(wt, "checkout", "-q", "master")
    _git(
        wt,
        "merge",
        "-q",
        "--no-ff",
        "later",
        "-m",
        "Merge pull request #90 from org/later\n\nFollow-up to (#7)\n",
    )
    bare = tmp_path / "mirror.git"
    _git(tmp_path, "clone", "-q", "--bare", str(wt), str(bare))
    return str(bare), sha7


@pytest.fixture
def fresh_mirror_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review_brief_sources, "_mirror_cache", {})


@pytest.mark.asyncio
async def test_mirror_merge_facts_finds_the_merge_by_subject_not_body_and_caches(
    mirror: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    fresh_mirror_cache: None,
) -> None:
    git_dir, sha7 = mirror
    from server.code.app.index import gitrepo

    calls: list[tuple[str, ...]] = []
    real_run = gitrepo.run

    async def counting_run(gd: str, *args: str, **kw: Any) -> str:
        calls.append(args)
        return await real_run(gd, *args, **kw)

    monkeypatch.setattr(gitrepo, "run", counting_run)
    facts = await review_brief_sources.mirror_merge_facts(git_dir, "master", 7)
    assert facts is not None
    # the merge OF #7, not the later merge whose body cites (#7) and comes first
    assert facts["merge_sha"] == sha7 and facts["pr_number"] == 7
    assert facts["title"] == "Fix the seventh thing" and facts["files"] == ["src/f.cpp"]
    assert facts["subject"] == "Merge pull request #7 from org/fix-7"
    assert len(calls) == 1 and calls[0][:2] == ("log", "--first-parent")
    assert "--grep=Merge pull request #7 from" in calls[0] and "--grep=(#7)" in calls[0]
    # #77's squash subject is its own PR, not a prefix match on #7
    facts77 = await review_brief_sources.mirror_merge_facts(git_dir, "master", 77)
    assert (
        facts77 is not None
        and facts77["title"] == "Seventy-seven (#77)"
        and facts77["files"] == ["b.txt"]
    )
    # a hit is cached: no second git call for #7
    again = await review_brief_sources.mirror_merge_facts(git_dir, "master", 7)
    assert again == facts and len(calls) == 2
    # a miss is cached for a while, then retried
    clock = [1000.0]
    assert (
        await review_brief_sources.mirror_merge_facts(git_dir, "master", 8, now=lambda: clock[0])
        is None
    )
    assert (
        await review_brief_sources.mirror_merge_facts(git_dir, "master", 8, now=lambda: clock[0])
        is None
    )
    assert len(calls) == 3
    clock[0] += settings.code_review_brief_mirror_miss_ttl_s + 1
    assert (
        await review_brief_sources.mirror_merge_facts(git_dir, "master", 8, now=lambda: clock[0])
        is None
    )
    assert len(calls) == 4
    # a sha-only read names its PR from the subject
    by_sha = await review_brief_sources.mirror_commit_facts(git_dir, sha7)
    assert by_sha is not None and by_sha["pr_number"] == 7 and by_sha["files"] == ["src/f.cpp"]
    root = (await real_run(git_dir, "rev-list", "--max-parents=0", "master")).strip()
    # the root has no ^1; the error propagates to the handler's catch
    with pytest.raises(gitrepo.GitError):
        await review_brief_sources.mirror_commit_facts(git_dir, root)


@pytest.mark.asyncio
async def test_mirror_cache_is_bounded_and_evicts_the_oldest_entry(
    monkeypatch: pytest.MonkeyPatch, fresh_mirror_cache: None
) -> None:
    from server.code.app.index import gitrepo

    async def no_merge(*_a: Any, **_k: Any) -> str:
        return ""

    monkeypatch.setattr(gitrepo, "run", no_merge)
    monkeypatch.setattr(review_brief_sources, "_MIRROR_CACHE_MAX", 3)
    for n in range(1, 5):
        assert await review_brief_sources.mirror_merge_facts("g", "master", n) is None
    assert list(review_brief_sources._mirror_cache) == [
        ("g", "master", 2),
        ("g", "master", 3),
        ("g", "master", 4),
    ]


# ---- handler: ledger miss → mirror -------------------------------------------


class _LedgerlessRepo:
    def __init__(self) -> None:
        self.reads: list[tuple[int, str]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def review_brief(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        self.reads.append((pr_number, merge_sha))
        return None

    async def pr_merge_facts(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        return None  # the PR predates the backfill horizon

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return SHA


MIRROR_FACTS = {
    "pr_number": 74989,
    "merge_sha": MIRROR_SHA,
    "title": "Fix SSH exit codes",
    "subject": "Merge pull request #74989 from ClickHouse/fix-ssh",
    "files": ["src/Server/SSHPtyHandler.cpp"],
}


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "repo": _LedgerlessRepo(),
        "mirror": [],
        "tracker": (
            "served",
            {
                "title": "Fix SSH client exit codes",
                "reviewers": ["oliver", "amir"],
                "author": "nora",
                "has_test": 1,
                "is_backport_pr": 0,
            },
        ),
    }
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(
        h, "schedule_code_trace", lambda **kw: state.setdefault("traces", []).append(kw)
    )
    monkeypatch.setattr(h, "set_code_span_attributes", lambda **kw: None)
    monkeypatch.setattr(h, "CodeRepo", lambda: state["repo"])
    import server.code.domain.index.repos as repos_mod

    class _Cfg:
        git_dir = "/nonexistent/mirror.git"
        git_branch = "master"
        path_prefixes = ("src/", "tests/")

    monkeypatch.setattr(repos_mod, "repo_for_namespace", lambda o, n: _Cfg())

    async def _mirror(
        git_dir: str, branch: str, pr_number: int, **kw: Any
    ) -> dict[str, Any] | None:
        state["mirror"].append((git_dir, branch, pr_number))
        return dict(MIRROR_FACTS) if pr_number == 74989 else None

    async def _by_sha(git_dir: str, sha: str, **kw: Any) -> dict[str, Any] | None:
        state["mirror"].append((git_dir, "sha", sha))
        return dict(MIRROR_FACTS) if sha == MIRROR_SHA else None

    async def _tracker(org: str, ns: str, pr_number: int) -> tuple[str, dict[str, Any]]:
        return state["tracker"]

    monkeypatch.setattr(review_brief_sources, "mirror_merge_facts", _mirror)
    monkeypatch.setattr(review_brief_sources, "mirror_commit_facts", _by_sha)
    monkeypatch.setattr(review_brief_sources, "tracker_pr_facts", _tracker)
    monkeypatch.setattr(h, "call_code_op", _Calls())
    monkeypatch.setattr(
        review_brief_build,
        "datetime",
        type("D", (), {"now": staticmethod(lambda tz=None: _fixed_now())}),
    )

    async def _git_run(git_dir: str, *args: str, **kw: Any) -> str:
        if args[0] == "rev-parse":
            return PARENT + "\n"
        return DIFF

    import server.code.app.index.gitrepo as gitrepo_mod

    monkeypatch.setattr(gitrepo_mod, "run", _git_run)
    import server.code.app.index.packs as packs_mod

    monkeypatch.setattr(packs_mod, "tracker_section_rows", _tracker_not_bound)
    return state


@pytest.mark.asyncio
async def test_ledger_miss_builds_from_the_mirror_discloses_it_and_stores_by_default(
    wired: dict[str, Any],
) -> None:
    out = await h.handle_code_review_brief(
        CodeReviewBriefInput(pr_number=74989, tier="full", consumer="clickgap")
    )
    assert out.source == "built_from_mirror" and out.ledger == "absent" and out.stored is True
    assert out.pr_number == 74989 and out.merge_sha == MIRROR_SHA
    assert wired["mirror"] == [("/nonexistent/mirror.git", "master", 74989)]
    assert out.provenance == {
        "source": "built_from_mirror",
        "ledger": "absent",
        "merge_commit": "mirror_first_parent",
        "files": "mirror_diff",
        "title": "tracker",
        "tracker": "served",
        "reviewers": ["oliver", "amir"],
        "author": "nora",
        "has_test": 1,
        "is_backport_pr": 0,
    }
    assert set(out.sections) == set(rb.SERVED_ORDER)
    assert out.decisions["randomized_setting"] is True
    repo = wired["repo"]
    assert [t for t, _ in repo.inserted] == ["code_review_briefs"]
    row = repo.inserted[0][1][0]
    assert row["pr_number"] == 74989 and row["merge_sha"] == MIRROR_SHA
    import json

    stored = json.loads(row["payload"])
    assert (
        stored["provenance"]["ledger"] == "absent"
        and stored["title"] == "Fix SSH client exit codes"
    )
    assert (
        wired["traces"][-1]["route"] == "brief_built_from_mirror"
        and wired["traces"][-1]["abstain"] is False
    )
    # the stored row, read back, is precomputed but still says the ledger was absent
    repo.stored = row

    async def _read(
        org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any]:
        return row

    repo.review_brief = _read  # type: ignore[method-assign]
    out2 = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=74989))
    assert out2.source == "precomputed" and out2.ledger == "absent" and out2.stored is False
    assert out2.provenance["reviewers"] == ["oliver", "amir"] and len(repo.inserted) == 1


@pytest.mark.asyncio
async def test_measurement_consumer_and_store_false_never_store(wired: dict[str, Any]) -> None:
    repo = wired["repo"]
    out = await h.handle_code_review_brief(
        CodeReviewBriefInput(pr_number=74989, consumer="eval-b12")
    )
    assert out.source == "built_from_mirror" and out.stored is False and repo.inserted == []
    out = await h.handle_code_review_brief(
        CodeReviewBriefInput(pr_number=74989, consumer="clickgap", store=False)
    )
    assert out.source == "built_from_mirror" and out.stored is False and repo.inserted == []
    for label in ("*", "-", "latency-bench"):
        out = await h.handle_code_review_brief(
            CodeReviewBriefInput(pr_number=74989, consumer=label)
        )
        assert out.stored is False and repo.inserted == []
    out = await h.handle_code_review_brief(
        CodeReviewBriefInput(pr_number=74989, consumer="clickgap", store=True)
    )
    assert out.stored is True and len(repo.inserted) == 1


@pytest.mark.asyncio
async def test_tracker_absent_leaves_the_mirror_title_and_no_reviewers(
    wired: dict[str, Any],
) -> None:
    wired["tracker"] = ("not_bound", {})
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=74989, consumer="eval-x"))
    assert out.provenance["title"] == "mirror" and out.provenance["tracker"] == "not_bound"
    assert out.provenance["reviewers"] == [] and out.provenance["has_test"] == -1
    wired["tracker"] = ("restricted", {})
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=74989, consumer="eval-x"))
    assert out.provenance["tracker"] == "restricted" and out.provenance["reviewers"] == []


@pytest.mark.asyncio
async def test_a_sha_only_read_without_a_ledger_row_asks_the_mirror_for_the_commit(
    wired: dict[str, Any],
) -> None:
    out = await h.handle_code_review_brief(
        CodeReviewBriefInput(merge_sha=MIRROR_SHA.upper(), consumer="eval-x")
    )
    assert out.source == "built_from_mirror" and out.pr_number == 74989 and out.ledger == "absent"
    assert wired["mirror"] == [("/nonexistent/mirror.git", "sha", MIRROR_SHA)]


@pytest.mark.asyncio
async def test_mirror_miss_flag_off_and_git_failure_are_missing(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=1, consumer="eval-x"))
    assert (
        out.source == "missing"
        and out.ledger == ""
        and out.provenance == {}
        and out.stored is False
    )
    assert wired["traces"][-1]["abstain"] is True

    async def _boom(*a: Any, **kw: Any) -> dict[str, Any] | None:
        raise RuntimeError("git timed out")

    monkeypatch.setattr(review_brief_sources, "mirror_merge_facts", _boom)
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=74989, consumer="eval-x"))
    assert out.source == "missing" and wired["repo"].inserted == []


# ---- the ledger path is byte-identical to the base brief ---------------------


@pytest.mark.asyncio
async def test_ledger_backed_build_carries_no_provenance_and_serialises_as_before() -> None:
    payload = await review_brief_build.build_brief(
        rb.BriefInputs(
            org=ORG,
            ns=NS,
            pr_number=117354,
            merge_sha=SHA,
            title="t",
            changed_paths=[],
            diff=DIFF,
            parent_sha=PARENT,
            indexed_commit=SHA,
            path_prefixes=("src/",),
        ),
        call=_Calls(),
        repo=_Repo(),
        tracker_rows=_tracker_not_bound,
        now=_fixed_now,
    )
    assert "provenance" not in payload and "stored" not in payload
    out = CodeReviewBriefOutput(pr_number=1, merge_sha=SHA, source="built")
    assert out.ledger == "" and out.provenance == {} and out.stored is False
    assert CodeReviewBriefInput(pr_number=1).store is True
    assert h._ledger_of("built", {}) == "present" and h._ledger_of("precomputed", {}) == ""
    assert h._ledger_of("precomputed", {"ledger": "absent"}) == "absent"
    assert h._ledger_of("missing", {}) == ""


def test_sdk_body_is_unchanged_unless_store_is_false() -> None:
    from sdk.python.loom.code import CodeClient

    posted: list[tuple[str, dict[str, Any]]] = []

    class _Transport:
        async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            posted.append((path, body))
            return {}

    class _Agent:
        transport = _Transport()

    client = CodeClient(_Agent())  # type: ignore[arg-type]
    asyncio.run(client.review_brief(74989, tier="full"))
    assert posted[-1] == (
        "/v1/code.review_brief",
        {"pr_number": 74989, "tier": "full", "sections": []},
    )
    asyncio.run(client.review_brief(74989, store=False))
    assert posted[-1][1] == {
        "pr_number": 74989,
        "tier": "standard",
        "sections": [],
        "store": False,
    }
