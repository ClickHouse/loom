"""A duplicate check says whether the reported issue is fixed,
by which PR, in which release — and bisect uses that fix on its own."""

from __future__ import annotations

import asyncio
import types
from datetime import datetime
from typing import Any

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.tracker.api.models import MAX_FRAME_CHARS
from server.tracker.app import dupcheck_links as dl
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source import contract_reports as reports
from tests.unit.test_tracker_source import ORG, PUB, REPO, _cfg, _Upstream  # noqa: F401

pytestmark = pytest.mark.usefixtures("mirror_here")


def _pr(n: int, **kw: Any) -> dict[str, Any]:
    base = {
        "pr_number": n,
        "title": f"PR {n}",
        "state": "closed",
        "base_branch": "master",
        "minor_version": "",
        "is_merged": 1,
        "is_backport_pr": 0,
        "merged_at": datetime(2026, 9, 23, 2, 50),
    }
    return {**base, **kw}


# -- pure: which links a row shows, the release, the sentence -----------------


def test_a_merged_trunk_fix_leads_then_an_open_pr_and_rows_stay_lean() -> None:
    prs = [
        _pr(10, is_merged=0, state="open", minor_version="26.10"),
        _pr(7, minor_version="26.8"),
        _pr(12, minor_version="26.9"),
        _pr(20, is_backport_pr=1, base_branch="26.8", minor_version="26.8"),
    ]
    links = dl.pick_links(prs)
    assert [lk["number"] for lk in links] == [12, 7]  # newest merged trunk fix first, capped at 2
    assert links[0] == {
        "number": 12,
        "title": "PR 12",
        "state": "merged",
        "merged_at": "2026-09-23",
        "fixed_in": "26.9",
    }
    assert dl.fixed_in(prs) == "26.8"  # the FIRST release carrying a fix
    assert dl.backported_to(prs) == ["26.8"]
    only_open = dl.pick_links([_pr(10, is_merged=0, state="open")])
    assert only_open == [{"number": 10, "title": "PR 10", "state": "open"}]


def test_a_blank_minor_version_claims_no_release_and_no_links_add_nothing() -> None:
    row: dict[str, Any] = {"number": 1}
    dl.attach(row, [_pr(5)])
    assert (
        row["links"][0]["number"] == 5
        and "fixed_in" not in row
        and "fixed_in" not in row["links"][0]
    )
    empty: dict[str, Any] = {"number": 2}
    dl.attach(empty, [])
    assert empty == {"number": 2}


def test_the_answer_says_reported_fixed_and_where() -> None:
    dup = {
        "verdict": "duplicate_of",
        "items": [
            {
                "number": 112376,
                "state": "closed",
                "links": [{"number": 117999, "state": "merged"}],
                "fixed_in": "26.10",
                "backported_to": ["26.8"],
            }
        ],
    }
    assert dl.answer_line(dup) == (
        "already reported as #112376 (closed); fixed by #117999, in 26.10 (backported to 26.8)"
    )
    wip = {
        "verdict": "duplicate_of",
        "items": [{"number": 5, "state": "open", "links": [{"number": 9, "state": "open"}]}],
    }
    assert dl.answer_line(wip) == "already reported as #5 (open); a fix is in progress: #9 (open)"
    bare = {"verdict": "duplicate_of", "items": [{"number": 5, "state": "closed"}]}
    assert dl.answer_line(bare).endswith("no fix PR is linked in the tracker")


def test_a_rerank_same_label_is_a_lead_and_never_this_bugs_report() -> None:
    row = {
        "number": 7,
        "state": "closed",
        "rerank": {"label": "same", "confidence": 0.95},
        "links": [{"number": 8, "state": "merged"}],
    }
    out = {"verdict": "none", "nearest": [row]}
    assert dl.lead_issue(out) is None
    assert dl.answer_line(out, 0.9) == (
        "no calibrated duplicate; nearest reported: #7 (closed, fixed by #8) — may be related, "
        "check it"
    )
    weak = {"verdict": "none", "nearest": [{**row, "rerank": {"label": "same", "confidence": 0.5}}]}
    assert dl.answer_line(weak, 0.9) == ""
    related = {
        "verdict": "none",
        "nearest": [{**row, "rerank": {"label": "related", "confidence": 1}}],
    }
    assert dl.answer_line(related, 0.9) == ""
    prs = {"verdict": "none", "related_prs": [{"number": 42, "state": "merged"}]}
    assert dl.answer_line(prs, 0.9) == (
        "no reported issue matches; a PR may already address this: #42 (merged)"
    )


def test_related_prs_are_lean_capped_and_never_repeat_a_linked_fix() -> None:
    pc = types.SimpleNamespace(
        model_dump=lambda: {
            "verdict": "related",
            "items": [
                {
                    "pr_number": n,
                    "title": "t",
                    "state": "merged",
                    "relation": "related",
                    "score": 0.3,
                    "merged_at": "2026-09-01 10:00:00",
                    "evidence": ["x"] * 9,
                }
                for n in (1, 2, 3, 4, 5)
            ],
        }
    )
    rows = dl.related_prs(pc, exclude={2})
    assert [r["number"] for r in rows] == [1, 3, 4]
    assert set(rows[0]) == {"number", "title", "state", "relation", "score", "merged_at"}
    assert rows[0]["merged_at"] == "2026-09-01"
    none = types.SimpleNamespace(
        model_dump=lambda: {"verdict": "none", "items": [{"pr_number": 1}]}
    )
    assert dl.related_prs(none, exclude=set()) == []


def test_a_replay_sees_only_fixes_merged_before_it() -> None:
    wall = datetime(2026, 9, 1)
    prs = [
        _pr(1, merged_at=datetime(2026, 8, 1)),
        _pr(2, merged_at=datetime(2026, 9, 20)),
        _pr(3, is_merged=0, state="open", merged_at=None),
    ]
    assert [p["pr_number"] for p in dl.as_of(prs, wall)] == [1, 3]
    assert dl.as_of(prs, None) == prs


def test_a_row_names_its_repository_from_its_url() -> None:
    url = "https://github.com/ClickHouse/example-private/issues/9"
    assert dl.repo_of({"url": url}) == "ClickHouse/example-private"
    assert dl.repo_of({"number": 9}, "ClickHouse/ClickHouse") == "ClickHouse/ClickHouse"


# -- the batched read ------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_statement_reads_every_shown_issues_fixes_and_maps_them_back(monkeypatch) -> None:
    from server.tracker.domain.namespaces import doc_id, ref_of

    iid = doc_id("github_issue", ref_of(REPO, 112376))
    rows = [
        {
            "issue_id": iid,
            "pr_number": 117999,
            "title": "Let a user cancel",
            "state": "closed",
            "base_branch": "master",
            "minor_version": "26.10",
            "is_merged": 1,
            "is_backport_pr": 0,
            "merged_at": datetime(2026, 9, 23),
        },
        # the same edge written twice upstream
        {
            "issue_id": iid,
            "pr_number": 117999,
            "title": "Let a user cancel",
            "state": "closed",
            "base_branch": "master",
            "minor_version": "26.10",
            "is_merged": 1,
            "is_backport_pr": 0,
            "merged_at": datetime(2026, 9, 23),
        },
    ]
    up = _Upstream(rows)
    monkeypatch.setattr(qi, "query_rows", up)
    monkeypatch.setattr(reports, "cached", lambda cred: None)
    s = src.TrackerSource(ORG, PUB)
    out = await s.fix_prs_of_issues(ORG, PUB, [(REPO, 112376), ("Other/repo", 5), (REPO, 0)])
    assert list(out) == [(REPO, 112376)] and [p["pr_number"] for p in out[(REPO, 112376)]] == [
        117999
    ]
    sql, params, _ = up.calls[-1]
    assert "rel_type = 'fixes'" in sql and params["ids"] == [iid]
    assert (
        "closed_at AS merged_at" in sql
    )  # merged_at is an optional column of the tracker contract
    assert await s.fix_prs_of_issues(ORG, PUB, []) == {}


# -- the handler: links on every row, related PRs only without a lead ------------


def _output(**kw: Any) -> Any:
    from server.tracker.api.models import TrackerDuplicateCheckOutput

    return TrackerDuplicateCheckOutput(**kw)


@pytest.mark.asyncio
async def test_every_shown_row_gets_its_links_and_the_answer_leads(monkeypatch) -> None:
    from server.tracker.app import dupcheck as h

    async def fixes(self, org, ns, issues):
        return {(REPO, 112376): [_pr(117999, minor_version="26.10")]}

    monkeypatch.setattr(src.TrackerSource, "fix_prs_of_issues", fixes)
    monkeypatch.setattr(src.TrackerSource, "known", property(lambda self: True))
    monkeypatch.setattr(src.TrackerSource, "repos", lambda self: [REPO])
    out = _output(
        verdict="none",
        nearest=[
            {
                "number": 112376,
                "state": "closed",
                "namespace": PUB,
                "rerank": {"label": "same", "confidence": 0.95},
            },
            {"number": 7, "state": "open", "namespace": PUB},
        ],
    )

    async def pr_search() -> Any:
        return types.SimpleNamespace(
            model_dump=lambda: {
                "verdict": "related",
                "items": [{"pr_number": 1, "title": "t", "state": "merged"}],
            }
        )

    task = asyncio.ensure_future(pr_search())
    got = await h._with_links(out, ORG, PUB, task)
    near = got.nearest
    assert near[0]["links"][0]["number"] == 117999 and near[0]["fixed_in"] == "26.10"
    assert "links" not in near[1]
    # the rerank's `same` is a lead, never the verdict (the strict bar is off)
    assert got.answer == (
        "no calibrated duplicate; nearest reported: #112376 (closed, fixed by #117999 in 26.10)"
        " — may be related, check it"
    )


@pytest.mark.asyncio
async def test_with_no_lead_the_prs_that_may_address_it_ride_along(monkeypatch) -> None:
    from server.tracker.app import dupcheck as h

    async def fixes(self, org, ns, issues):
        return {}

    monkeypatch.setattr(src.TrackerSource, "fix_prs_of_issues", fixes)
    monkeypatch.setattr(src.TrackerSource, "known", property(lambda self: True))
    monkeypatch.setattr(src.TrackerSource, "repos", lambda self: [REPO])

    async def pr_search() -> Any:
        return types.SimpleNamespace(
            model_dump=lambda: {
                "verdict": "related",
                "items": [{"pr_number": 42, "title": "Fix it", "state": "open"}],
            }
        )

    got = await h._with_links(
        _output(verdict="none", nearest=[{"number": 7}]),
        ORG,
        PUB,
        asyncio.ensure_future(pr_search()),
    )
    assert [p["number"] for p in got.related_prs] == [42]
    assert got.answer == "no reported issue matches; a PR may already address this: #42 (open)"


@pytest.mark.asyncio
async def test_a_failed_links_read_or_a_failed_pr_search_leave_the_answer_as_it_was(
    monkeypatch,
) -> None:
    from server.tracker.app import dupcheck as h

    async def boom(self, org, ns, issues):
        raise TrackerUnavailable("down")

    monkeypatch.setattr(src.TrackerSource, "fix_prs_of_issues", boom)
    monkeypatch.setattr(src.TrackerSource, "known", property(lambda self: True))
    monkeypatch.setattr(src.TrackerSource, "repos", lambda self: [REPO])

    async def failing() -> Any:
        return None

    out = _output(verdict="duplicate_of", items=[{"number": 5, "state": "closed"}])
    got = await h._with_links(out, ORG, PUB, asyncio.ensure_future(failing()))
    assert "links" not in got.items[0]
    assert got.answer == "already reported as #5 (closed); no fix PR is linked in the tracker"
    unavailable = _output(verdict="none", tracker_unavailable=True)
    assert (await h._with_links(unavailable, ORG, PUB, None)) is unavailable


def test_the_search_legs_budget_has_no_room_for_the_pr_search() -> None:
    import inspect

    from server.memory.app.loom_search import tracker_leg

    assert "related_prs=False" in inspect.getsource(tracker_leg.tracker_leg)
    sec = tracker_leg.lean_tracker_section(
        _output(
            verdict="duplicate_of",
            answer="already reported as #5 (closed); fixed by #6",
            items=[{"number": 5, "links": [{"number": 6, "state": "merged"}], "fixed_in": "26.8"}],
        ),
        {},
    )
    assert sec["answer"].endswith("fixed by #6") and sec["results"][0]["fixed_in"] == "26.8"
    assert (
        tracker_leg.tracker_text_lines(sec)[1] == "  already reported as #5 (closed); fixed by #6"
    )


# -- bisect: the matched issue's fix, on its own ---------------------------------


def _bisect_input(**kw: Any) -> Any:
    from server.code.api.models_history import CodeBisectCandidatesInput

    base: dict[str, Any] = {
        "org": ORG,
        "namespace": "code-clickhouse",
        "bad_version": "26.4",
        "text": "has() over JSON dynamic subcolumns got 4x slower",
    }
    return CodeBisectCandidatesInput(**{**base, **kw})


def test_bisect_asks_the_duplicate_check_only_when_nothing_names_a_fix(monkeypatch) -> None:
    from server.code.app import bisect_fix

    assert (
        bisect_fix._start_dupcheck_fix(_bisect_input(fix_pr=5), ORG, "code-clickhouse", []) is None
    )
    assert (
        bisect_fix._start_dupcheck_fix(_bisect_input(issue_number=5), ORG, "code-clickhouse", [])
        is None
    )
    assert (
        bisect_fix._start_dupcheck_fix(_bisect_input(text=""), ORG, "code-clickhouse", []) is None
    )
    assert (
        bisect_fix._start_dupcheck_fix(_bisect_input(bad_version=""), ORG, "code-clickhouse", [])
        is None
    )
    named = _bisect_input(text="slower since #100730 landed (regression from #100730)")
    assert bisect_fix._start_dupcheck_fix(named, ORG, "code-clickhouse", []) is None


@pytest.mark.asyncio
async def test_bisect_takes_a_strong_matchs_merged_fix_and_fails_open(monkeypatch) -> None:
    from server.code.app import bisect_fix
    from server.tracker.app import dupcheck as tdc

    seen: dict[str, Any] = {}

    async def dupcheck(inp: Any) -> Any:
        seen["inp"] = inp
        return _output(
            verdict="duplicate_of",
            items=[
                {
                    "number": 107942,
                    "namespace": PUB,
                    "state": "closed",
                    "fixed_in": "26.6",
                    "fixed_in_source": "release_tag",
                    "links": [{"number": 107997, "state": "merged"}],
                }
            ],
        )

    monkeypatch.setattr(tdc, "handle_tracker_duplicate_check", dupcheck)
    long_line = "a" * (MAX_FRAME_CHARS + 1)
    task = bisect_fix._start_dupcheck_fix(
        _bisect_input(
            before="2026-06-01 00:00:00", paths=[long_line, "src/Columns/ColumnDynamic.cpp"]
        ),
        ORG,
        "code-clickhouse",
        ["DB::ColumnDynamic::get", long_line],
    )
    got = await bisect_fix._dupcheck_fix(task)
    assert got["issue"] == 107942 and got["fix_pr"] == 107997 and got["match"] == "duplicate"
    assert "fixed by #107997" in got["note"]
    assert seen["inp"].related_prs is False and seen["inp"].as_of == "2026-06-01 00:00:00"
    assert seen["inp"].rerank is False
    # a line the check would refuse is dropped, not a failed check
    assert seen["inp"].frames == ["DB::ColumnDynamic::get"]
    assert seen["inp"].paths == ["src/Columns/ColumnDynamic.cpp"]

    async def unmatched(inp: Any) -> Any:
        return _output(
            verdict="none", nearest=[{"number": 1, "links": [{"number": 2, "state": "merged"}]}]
        )

    monkeypatch.setattr(tdc, "handle_tracker_duplicate_check", unmatched)
    assert (
        await bisect_fix._dupcheck_fix(
            bisect_fix._start_dupcheck_fix(_bisect_input(), ORG, "c", [])
        )
        == {}
    )

    async def broken(inp: Any) -> Any:
        raise RuntimeError("tracker down")

    monkeypatch.setattr(tdc, "handle_tracker_duplicate_check", broken)
    assert (
        await bisect_fix._dupcheck_fix(
            bisect_fix._start_dupcheck_fix(_bisect_input(), ORG, "c", [])
        )
        == {}
    )
    assert await bisect_fix._dupcheck_fix(None) == {}


# -- the release the fix ships in, from the mirror --------------------------------


@pytest.mark.asyncio
async def test_the_mirror_names_the_first_release_and_corrects_the_trackers_estimate(
    monkeypatch,
) -> None:
    from server.code.app.index import gitrepo
    from server.code.domain.index import repos
    from server.code.store import index as code_repo

    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [
            types.SimpleNamespace(org=ORG, namespace="code-clickhouse", git_dir="/m/clickhouse.git")
        ],
    )

    async def shas(self, org, ns, prs):
        return {107675: "a" * 40, 3: "b" * 40, 4: "c" * 40}

    monkeypatch.setattr(code_repo.CodeRepo, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(code_repo.CodeRepo, "merge_commits_of", shas)
    answers = {"a" * 40: "26.7", "b" * 40: "", "c" * 40: None}

    async def release(git_dir, sha, *, timeout_s=3.0):
        assert git_dir == "/m/clickhouse.git"
        if answers[sha] is None:
            raise gitrepo.GitError("timeout")
        return answers[sha]

    monkeypatch.setattr(gitrepo, "release_containing", release)
    near_cut = {
        "number": 81631,
        "links": [{"number": 107675, "state": "merged", "fixed_in": "26.6"}],
        "fixed_in": "26.6",
    }  # merged just after the 26.6 branch cut
    unreleased = {"number": 2, "links": [{"number": 3, "state": "merged"}]}
    broken = {"number": 5, "links": [{"number": 4, "state": "merged"}], "fixed_in": "26.1"}
    bare = {"number": 6}
    await dl.release_of_merges(ORG, "code-clickhouse", [near_cut, unreleased, broken, bare])
    assert near_cut["fixed_in"] == "26.7" and near_cut["fixed_in_source"] == "release_tag"
    assert "fixed_in" not in unreleased and unreleased["fixed_in_source"] == "not_released"
    assert broken["fixed_in"] == "26.1" and broken["fixed_in_source"] == "tracker"
    assert bare == {"number": 6}
    line = dl.answer_line({"verdict": "duplicate_of", "items": [{**unreleased, "state": "closed"}]})
    assert line.endswith("fixed by #3, not in a release yet")


@pytest.mark.asyncio
async def test_without_a_code_namespace_or_mirror_the_estimate_stands(monkeypatch) -> None:
    from server.code.domain.index import repos

    monkeypatch.setattr(repos, "configured_repos", lambda: [])
    row = {"number": 1, "links": [{"number": 2, "state": "merged"}], "fixed_in": "26.4"}
    await dl.release_of_merges(ORG, "", [row])
    assert row["fixed_in"] == "26.4" and row["fixed_in_source"] == "tracker"
    other = {"number": 1, "links": [{"number": 2, "state": "merged"}], "fixed_in": "26.4"}
    await dl.release_of_merges(ORG, "code-unknown", [other])
    assert other["fixed_in"] == "26.4"


@pytest.mark.asyncio
async def test_release_containing_reads_the_lowest_stable_or_lts_line(monkeypatch) -> None:
    from server.code.app.index import gitrepo

    seen: list[tuple[str, ...]] = []

    async def fake_run(git_dir, *args, timeout_s=3.0):
        seen.append(args)
        return "v26.8.2.3-stable\nv26.7.1.1315-stable\nv26.8.1.1-lts\n"

    monkeypatch.setattr(gitrepo, "run", fake_run)
    assert await gitrepo.release_containing("/g", "a" * 40) == "26.7"
    assert seen[0][:3] == ("tag", "--contains", "a" * 40) and "v*-stable" in seen[0]

    async def none_yet(git_dir, *args, timeout_s=3.0):
        return ""

    monkeypatch.setattr(gitrepo, "run", none_yet)
    assert await gitrepo.release_containing("/g", "a" * 40) == ""


def test_a_fix_that_shipped_before_the_failing_release_is_some_older_bugs() -> None:
    from server.code.app import bisect_fix

    old = {"fixed_in": "25.5", "fixed_in_source": "release_tag"}
    assert bisect_fix._fix_after_regression(old, "25.11") is False
    assert bisect_fix._fix_after_regression(
        {"fixed_in": "26.6", "fixed_in_source": "release_tag"}, "26.4"
    )
    assert (
        bisect_fix._fix_after_regression({"fixed_in": "26.4", "fixed_in_source": "tracker"}, "26.4")
        is False
    )
    assert bisect_fix._fix_after_regression(
        {"fixed_in": "26.5", "fixed_in_source": "tracker"}, "26.4"
    )
    assert bisect_fix._fix_after_regression({"fixed_in_source": "not_released"}, "26.8")
    assert bisect_fix._fix_after_regression({}, "26.8") is False  # no release known: no claim
    assert bisect_fix._fix_after_regression({"fixed_in": "26.6"}, "") is False


def test_bisect_borrows_only_what_the_answer_would_state() -> None:
    anchored = {"verdict": "none", "nearest": [{"number": 1, "why": ["lexical", "vector"]}]}
    assert dl.lead_issue(anchored) is None
    labelled = {
        "verdict": "none",
        "nearest": [
            {"number": 1, "why": ["vector"], "rerank": {"label": "same", "confidence": 0.95}}
        ],
    }
    assert dl.lead_issue(labelled) is None
    dup = {"verdict": "duplicate_of", "items": [{"number": 7}]}
    assert dl.lead_issue(dup) == {"number": 7}


def test_a_lead_is_phrased_as_a_lead_and_never_as_this_bugs_fix() -> None:
    from server.settings import settings

    out = {
        "verdict": "none",
        "nearest": [
            {
                "number": 61549,
                "state": "closed",
                "rerank": {"label": "same", "confidence": 0.92},
                "links": [{"number": 104648, "state": "merged"}],
                "fixed_in": "26.6",
            },
            {"number": 5, "state": "open", "rerank": {"label": "same", "confidence": 0.95}},
        ],
    }
    line = dl.answer_line(out, float(settings.tracker_dupcheck_lead_min))
    assert line == "no calibrated duplicate; nearest reported: #5 (open) — may be related, check it"
    assert "same defect" not in line and "already" not in line
    out["nearest"][1]["rerank"]["confidence"] = 0.5
    line = dl.answer_line(out, 0.9)
    assert line == (
        "no calibrated duplicate; nearest reported: #61549 (closed, fixed by #104648"
        " in 26.6) — may be related, check it"
    )
    assert dl.answer_line({"verdict": "none", "nearest": [{"number": 1}]}, 0.9) == ""


def test_a_remembered_answer_serves_a_report_with_the_same_words() -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.tracker.app import dupcheck as h

    def _as(subject: str) -> None:
        set_current_auth(
            AuthInfo(
                subject=subject,
                org=ORG,
                namespaces=("*",),
                roles=(Role.VIEWER,),
                is_service_account=False,
            )
        )

    h._RECENT.clear()
    title = "has over JSON dynamic subcolumns got 4x slower"
    out = _output(verdict="duplicate_of", items=[{"number": 5}])
    try:
        _as("alice")
        h.remember_answer(ORG, "code-a", "has() over JSON dynamic subcolumns got 4x slower", out)
        assert h.recent_answer(ORG, "code-a", title + "!", "") is out
        assert h.recent_answer(ORG, "code-a", "a different crash in MergeTree reads", "") is None
        assert h.recent_answer("other-org", "code-a", title, "") is None
        # the answer spans what alice could read and links fixes in code-a: served
        # neither to another principal nor for another code namespace
        assert h.recent_answer(ORG, "code-b", title, "") is None
        _as("bob")
        assert h.recent_answer(ORG, "code-a", title, "") is None
    finally:
        set_current_auth(None)
        h._RECENT.clear()


@pytest.mark.asyncio
async def test_a_slow_check_times_out_inside_its_budget(monkeypatch) -> None:
    import time as _t

    from server.code.app import bisect_fix
    from server.settings import settings

    monkeypatch.setattr(settings, "code_bisect_dupcheck_timeout_s", 1.0)

    async def slow() -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"fix_pr": 1}

    got = await bisect_fix._dupcheck_fix(asyncio.ensure_future(slow()), _t.monotonic() - 0.9)
    assert got == {"status": "timeout", "budget_s": 1.0}


@pytest.mark.asyncio
async def test_a_failed_bisect_cancels_the_duplicate_check_it_started(monkeypatch) -> None:
    """The check runs as a task beside the resolve reads; a stage that raises before the
    check is awaited must not leave it running."""
    import asyncio

    import server.tracker.app.binding as binding
    from server.code.app import bisect, bisect_fix
    from server.code.app import bisect as cb

    async def _check() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    async def _no_tracker(org: str, ns: str, *, hides_answer=True) -> str:
        return ""

    async def _window(*_a: Any, **_k: Any) -> Any:
        return None, None

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("symbol read failed")

    monkeypatch.setattr(cb, "require_code_intel", lambda: None)
    monkeypatch.setattr(cb, "resolve_code_identity", lambda org, ns: (org, ns))
    monkeypatch.setattr(binding, "authorized_tracker_namespace", _no_tracker)
    monkeypatch.setattr(bisect, "_time_window", _window)
    started: list[asyncio.Future[Any]] = []

    def _start(*_a: Any) -> asyncio.Future[Any]:
        started.append(asyncio.ensure_future(_check()))
        return started[0]

    monkeypatch.setattr(bisect_fix, "_start_dupcheck_fix", _start)
    monkeypatch.setattr(cb, "_start_dupcheck_fix", _start)
    monkeypatch.setattr(cb, "_paths_for_symbols", _boom)
    monkeypatch.setattr(cb, "CodeRepo", lambda: object())
    monkeypatch.setattr(bisect_fix, "CodeRepo", lambda: object())
    with pytest.raises(RuntimeError):
        await cb.handle_code_bisect_candidates(_bisect_input(symbols=["DB::X::f"]))
    await asyncio.sleep(0)
    assert started and started[0].cancelled()
