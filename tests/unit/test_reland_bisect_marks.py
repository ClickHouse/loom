"""A re-land keeps the reverted change alive (the reverted PR
is not demoted, the re-land is never a revert and inherits the original's
symbol marks), and `code.bisect_candidates` carries its marks — backport,
revert, reverted, re-land, whitespace / rename-only from a bounded diff,
`touches_symbol` from the QA diff's hunk headers, named in title — disclosed
per candidate, hard-demoted last, `marks=false` byte-identical to the
unmarked dicts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.code.app.bisect as hb
import server.code.app.blame as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeBisectCandidatesInput, CodeBlameRangeInput
from server.code.app.index.gitrepo import GitError
from server.code.domain.index import blame_introduced as intro
from server.code.domain.index import blame_range as br
from server.tracker.domain import bisect as bi
from tests.unit.test_code_blame_introduced import (
    IN_FUNCTION,
    NS,
    ORG,
    PATH,
    RENAME,
    S5,
    S6,
    S400,
    SUBSTANTIVE,
    _Cfg,
    _Git,
    _Repo,
    _row,
)
from tests.unit.test_code_blame_range import mirror_on_disk

SRC = "src/Storages/MergeTree/MergeTreeData.cpp"
QA = "tracker-clickhouse"
LWU = "Lightweight Updates v2"
REVERT_BODY = (
    "Related: https://github.com/ClickHouse/ClickHouse/pull/103182\nReverts #103182 "
    "(`Lightweight Updates v2`, merged as `70a0c0ff637`), which introduced a LOGICAL_ERROR"
)
RELAND_BODY = (
    "Related: https://github.com/ClickHouse/ClickHouse/pull/103182\nRe-lands #103182 "
    "(`Lightweight Updates v2`), which was reverted in #116005, together with a fix"
)


def _cand(n: int, title: str, *, lines: int = 0, merged: str, sha: str = "") -> br.Candidate:
    return br.Candidate(
        pr_number=n,
        title=title,
        merged_at=merged,
        lines_blamed=lines,
        merge_commit_sha=sha or f"{n:040d}",
        evidence="blame" if lines else "ledger",
        kind=bi.kind_of(title),
    )


# ---- pure rules -----------------------------------------------------------------


def test_titles_normalize_through_revert_and_reland_wrappers() -> None:
    assert intro.normalize_title('Revert "Lightweight Updates v2"') == "lightweight updates v2"
    assert intro.normalize_title("Lightweight Updates v2 (#116007)") == "lightweight updates v2"
    assert intro.normalize_title('Revert "Revert "The PromQL topk plan""') == "the promql topk plan"
    assert intro.normalize_title("Reland: “Fix   x” (#5)") == "fix x"
    assert (
        intro.normalize_title("Re-land of Fix x") == "fix x"
        and intro.normalize_title("Reapply Fix x") == "fix x"
    )
    assert (
        intro.normalize_title("Reverted paths are counted") == "reverted paths are counted"
    )  # a word, not a wrapper
    assert intro.is_reland_title('Revert "Revert x"') and intro.is_reland_title("Reland x")
    assert intro.is_reland_title("Re-landing x") and not intro.is_reland_title('Revert "x"')
    # parity: a third revert is a revert again (#96028 live), a fourth a re-land (#96088)
    assert intro.revert_depth('Revert "Revert "Revert "Merge pull request #93542"""') == 3
    assert not intro.is_reland_title('Revert "Revert "Revert "Merge pull request #93542"""')
    assert intro.is_reland_title('Revert "Revert "Revert "Revert "Merge pull request #93542""""')
    assert intro.is_revert('Revert "Revert "Revert "X"""', "Reverts ClickHouse/ClickHouse#96028")
    assert (
        intro.revert_depth("Revert Broken PRs: only treat failures") == 1
    )  # a tool's name: still counted


def test_a_reland_is_never_a_revert_whatever_its_body_says() -> None:
    # #116007's body says "reverted in #116005" — a revert by the body rule alone
    assert not intro.is_revert(LWU, RELAND_BODY)
    assert intro.is_revert('Revert "Lightweight Updates v2"', REVERT_BODY)
    assert not intro.is_revert("Reland x", "Reverts ClickHouse/ClickHouse#1")
    assert not intro.is_revert("Fix y", "This is a revert of the revert of #7")
    assert intro.is_revert("Undo", "This reverts commit abc")


def test_detect_relands_by_title_body_double_revert_and_phrase() -> None:
    pool = [
        (103182, LWU, "", 100),
        (116005, f'Revert "{LWU}"', REVERT_BODY, 200),
        (116007, LWU, RELAND_BODY, 300),
        (114326, 'Revert "The PromQL topk plan"', "Reverts ClickHouse/ClickHouse#114000", 150),
        (
            114409,
            'Revert "Revert "The PromQL topk plan""',
            "Reverts ClickHouse/ClickHouse#114326",
            160,
        ),
        (999, "Unrelated fix", "fixes #1", 400),
        # merged BEFORE the revert: the same title is the original, not a re-land
        (50, "Fix z", "", 10),
        (51, 'Revert "Fix z"', "Reverts #50", 20),
        # a backport of the re-landed title is never a re-land
        (52, f"Backport #116007 to 26.8: {LWU}", "", 350),
    ]
    got = intro.detect_relands(pool)
    assert got[116007] == intro.Reland(116007, original=103182, via=116005, evidence="title")
    assert got[114409] == intro.Reland(
        114409, original=114000, via=114326, evidence="double_revert"
    )
    assert set(got) == {116007, 114409}
    # the original outside the pool: the revert's quoted title still names the pair
    only = intro.detect_relands(pool[1:3])
    assert only[116007].original == 103182 and only[116007].via == 116005
    # body naming the reverted PR next to a re-land phrase, a different title
    body = intro.detect_relands(
        [
            (1, "A", "", 1),
            (2, 'Revert "A"', "Reverts #1", 2),
            (3, "Something else", "Brings back #1 with a fix", 3),
            (4, "Follow-up", "Related: https://github.com/x/y/pull/1\nFixes #9", 4),
            (5, "Re-add the test", "Re-adds the test that #2 reverted", 5),
        ]
    )
    assert body[3] == intro.Reland(3, original=1, via=2, evidence="body")
    assert 4 not in body  # a bare `Related:` link is a follow-up, not a re-land
    assert body[5] == intro.Reland(5, original=1, via=2, evidence="body")  # names the revert itself
    # an outright phrase with no revert in the pool
    phrase = intro.detect_relands([(9, "Fix q", "This relands #4 after the fix", 9)])
    assert phrase[9] == intro.Reland(9, original=4, via=0, evidence="body_phrase")
    assert intro.detect_relands([]) == {}


def test_marks_keep_the_reverted_pr_alive_and_the_reland_inherits_symbol_marks() -> None:
    cands = [
        _cand(116007, LWU, lines=3, merged="2026-08-25T13:02:48+00:00"),
        _cand(116005, f'Revert "{LWU}"', merged="2026-08-23T18:13:06+00:00"),
        _cand(103182, LWU, merged="2026-05-01T00:00:00+00:00"),
        _cand(700, "Touch", merged="2026-08-01T00:00:00+00:00"),
    ]
    marks = intro.mark_candidates(
        cands,
        bodies={
            116005: REVERT_BODY,
            116007: RELAND_BODY,
            103182: "updateLightweightImpl learns v2",
        },
        linked={},
        issue_titles={},
        words=[],
        frames=["updateLightweightImpl"],
        span_comment_prs=set(),
        tracker_names={},
        qa_ok=False,
        diff_kinds={},
    )
    assert marks[116005].demoted == "revert"
    assert marks[103182].demoted == "" and marks[103182].relanded_by == 116007
    assert marks[103182].touches_symbol == "named" and marks[103182].promoted == ["names_symbol"]
    assert marks[116007].demoted == "" and (marks[116007].reland_of, marks[116007].reland_via) == (
        103182,
        116005,
    )
    assert marks[116007].touches_symbol == "named" and marks[116007].promoted == [
        "reland_inherits_symbol"
    ]
    assert marks[116007].symbol_evidence
    order = [c.pr_number for c in intro.rank_introduced(cands, marks, blameless=False)]
    assert order == [116007, 103182, 700, 116005]  # the re-land (blamed) first; the revert last
    for c in cands:
        c.mark = marks[c.pr_number]
    d = {c.pr_number: c.as_dict() for c in cands}
    assert d[116007]["reland_of"] == 103182 and d[116007]["reland_via"] == 116005
    assert d[103182]["relanded_by"] == 116007 and "demoted" not in d[103182]
    assert "reland_of" not in d[700] and "relanded_by" not in d[700]
    # without the re-land in the pool the original stays demoted
    alone = intro.mark_candidates(
        cands[1:],
        bodies={116005: REVERT_BODY},
        linked={},
        issue_titles={},
        words=[],
        frames=[],
        span_comment_prs=set(),
        tracker_names={},
        qa_ok=False,
        diff_kinds={},
    )
    assert alone[103182].demoted == "reverted" and alone[103182].relanded_by == 0


# ---- blame_range handler --------------------------------------------------------


@pytest.fixture
def wired_blame(monkeypatch, tmp_path):
    h.reset_cache_for_tests()
    mirror_on_disk(monkeypatch, tmp_path)
    repo, git = _Repo(), _Git()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(h.gitrepo, "run", git)
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(repos, "configured_repos", lambda: [_Cfg(NS, "master")])
    monkeypatch.setattr(h.settings, "code_git_branch", "master")

    async def _unbound(org, code_ns):
        return ""

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _unbound)
    git.diffs[S400] = IN_FUNCTION
    repo.rows = [
        _row(
            400,
            "Add loading of parts",
            S400,
            datetime(2026, 1, 20),
            body_excerpt="loadDataParts learns",
        ),
        _row(
            500,
            'Revert "Add loading of parts"',
            S5,
            datetime(2026, 1, 22),
            body_excerpt="Reverts ClickHouse/ClickHouse#400",
        ),
        _row(
            600,
            "Add loading of parts",
            S6,
            datetime(2026, 1, 24),
            body_excerpt="Re-lands #400, which was reverted in #500, with a fix",
        ),
    ]
    return repo, git


@pytest.mark.asyncio
async def test_blame_range_marks_the_reland_and_keeps_the_original(wired_blame) -> None:
    repo, git = wired_blame
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=10,
            end_line=12,
            from_version="26.2",
            to_version="26.3",
        )
    )
    by = {c["pr_number"]: c for c in out.candidates}
    assert by[600]["reland_of"] == 400 and by[600]["reland_via"] == 500 and "demoted" not in by[600]
    assert by[400]["relanded_by"] == 600 and "demoted" not in by[400]
    assert by[500]["demoted"] == "revert"
    # the original's hunk header names the function; the re-land inherits it
    assert by[400]["touches_symbol"] == "true" and by[600]["touches_symbol"] == "true"
    assert by[600]["promoted"] == ["reland_inherits_symbol"]
    order = [c["pr_number"] for c in out.candidates]
    assert order.index(500) == len(order) - 1 and order.index(600) < order.index(400)
    assert out.legs["introduced"]["demoted"] == 1
    # `last_touched` never carries the marks
    assert all(
        set(r) == {"pr_number", "rank", "lines_blamed", "evidence"} for r in out.last_touched
    )


# ---- bisect: pure marks --------------------------------------------------------


def _bc(
    n: int,
    title: str,
    *,
    merged: str,
    score: float = 1.0,
    hunk: list[str] | None = None,
    named_by: str = "",
) -> bi.Candidate:
    return bi.Candidate(
        pr_number=n,
        title=title,
        author="a",
        merged_at=merged,
        minor_version="26.4",
        matched_paths=[SRC],
        matched_symbols=[],
        kind=bi.kind_of(title),
        score=score,
        hunk_symbols=hunk or [],
        named_by=named_by,
    )


def test_bisect_marks_and_marked_order() -> None:
    cands = [
        _bc(1, "Backport #9 to 26.4: Fix x", merged="2026-04-09T00:00:00", score=3.0),
        _bc(2, 'Revert "Speed up loading"', merged="2026-04-08T00:00:00", score=3.0),
        _bc(3, "Speed up loading", merged="2026-04-01T00:00:00", score=1.0),
        _bc(4, "Speed up loading", merged="2026-04-10T00:00:00", score=1.0, hunk=["loadDataParts"]),
        _bc(5, "Rename helper", merged="2026-04-07T00:00:00", score=2.0),
        _bc(6, "Make loadDataParts lazy", merged="2026-04-06T00:00:00", score=2.0),
        _bc(7, "Touch the file", merged="2026-04-05T00:00:00", score=2.25),
        _bc(
            8,
            "Refactor part loading",
            merged="2026-04-04T00:00:00",
            score=2.25,
            named_by="caused_by_edge",
        ),
    ]
    records = {
        2: {"body_excerpt": "Reverts ClickHouse/ClickHouse#3"},
        4: {"body_excerpt": "Re-lands #3"},
        6: {"body_excerpt": "x"},
    }
    marks = bi.mark_bisect_candidates(
        cands,
        records=records,
        frames=["loadDataParts"],
        diff_kinds={5: "rename_only"},
        diff_touch={7: False},
    )
    assert marks[1].demoted == "backport" and marks[2].demoted == "revert"
    assert (
        marks[3].demoted == "" and marks[3].relanded_by == 4 and marks[3].touches_symbol == "false"
    )
    assert (marks[4].reland_of, marks[4].reland_via) == (3, 2) and marks[4].touches_symbol == "true"
    assert marks[4].promoted == ["touches_symbol"]
    assert marks[5].demoted == "rename_only" and marks[5].meta
    assert marks[6].touches_symbol == "named" and marks[6].promoted == ["names_symbol"]
    assert (
        marks[7].touches_symbol == "false" and marks[8].touches_symbol == "unknown"
    )  # unscanned graph pick
    order = [c.pr_number for c in bi.rank_marked(cands, marks)]
    # undemoted by score + bonus: #6 (2.0 + .25 named), #7 (2.25) and the graph
    # pick #8 (2.25) tie with it — newest first; then #4 (1.0 + .5), #3 (1.0);
    # then the hard-demoted #1, #2, #5 by score
    assert order == [6, 7, 8, 4, 3, 1, 2, 5]
    for c in cands:
        c.mark = marks[c.pr_number]
    d = {c.pr_number: c.as_dict() for c in cands}
    assert d[4]["reland_of"] == 3 and d[4]["reland_via"] == 2 and d[3]["relanded_by"] == 4
    assert d[5]["demoted"] == "rename_only" and d[8].get("touches_symbol") is None
    assert "last_touched_rank" not in d[4]
    # no frames: no symbol mark at all
    bare = bi.mark_bisect_candidates(cands[:1], records={}, frames=[], diff_kinds={})
    assert bare[1].touches_symbol == "unknown"


# ---- bisect handler ---------------------------------------------------------------


class _BisectRepo:
    def __init__(self) -> None:
        self.records: dict[int, dict[str, Any]] = {}
        self.fail_records = False
        self.calls: list[str] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        return [{"path": SRC}] if "loadDataParts" in name else []

    async def pr_records_by_number(self, org, ns, numbers):
        self.calls.append("records")
        if self.fail_records:
            raise RuntimeError("ch down")
        return {n: self.records[n] for n in numbers if n in self.records}


class _BisectSource:
    def __init__(self, prs: list[dict[str, Any]]) -> None:
        self.prs = prs
        self.pr_calls = 0

    async def merged_prs_touching(
        self,
        org,
        ns,
        paths,
        versions,
        *,
        symbols=None,
        patterns=None,
        dirs=None,
        scope_patterns=None,
        pattern_weights=None,
        limit=20,
        before=None,
        after=None,
    ):
        self.pr_calls += 1
        return list(self.prs)


def _prs() -> list[dict[str, Any]]:
    return [
        {
            "pr_number": 500,
            "title": "Speed up loading",
            "author": "a",
            "matched_paths": [SRC],
            "matched_symbols": ["loadDataParts"],
            "hunk_symbols": ["loadDataParts"],
            "merged_at": datetime(2026, 3, 2),
            "minor_version": "26.3",
            "has_test": 0,
            "url": "u/500",
        },
        {
            "pr_number": 501,
            "title": 'Revert "Speed up loading"',
            "author": "b",
            "matched_paths": [SRC],
            "matched_symbols": ["loadDataParts"],
            "merged_at": datetime(2026, 3, 5),
            "minor_version": "26.3",
        },
        {
            "pr_number": 502,
            "title": "Speed up loading",
            "author": "a",
            "matched_paths": [SRC],
            "matched_symbols": ["loadDataParts"],
            "merged_at": datetime(2026, 3, 9),
            "minor_version": "26.3",
        },
        {
            "pr_number": 503,
            "title": "Reformat",
            "author": "c",
            "matched_paths": [SRC],
            "matched_symbols": [],
            "merged_at": datetime(2026, 3, 10),
            "minor_version": "26.3",
        },
        {
            "pr_number": 504,
            "title": "Fix crash",
            "author": "d",
            "matched_paths": [SRC],
            "matched_symbols": ["loadDataParts"],
            "merged_at": datetime(2026, 3, 1),
            "minor_version": "26.3",
        },
    ]


@pytest.fixture
def wired_bisect(monkeypatch, tmp_path):
    h.reset_cache_for_tests()
    mirror_on_disk(monkeypatch, tmp_path)
    repo = _BisectRepo()
    repo.records = {
        500: {"title": "Speed up loading", "body_excerpt": "", "merge_commit_sha": b"5" * 40},
        501: {
            "title": 'Revert "Speed up loading"',
            "body_excerpt": "Reverts ClickHouse/ClickHouse#500",
            "merge_commit_sha": b"6" * 40,
        },
        502: {
            "title": "Speed up loading",
            "body_excerpt": "Re-lands #500 after fixing the race",
            "merge_commit_sha": b"7" * 40,
        },
        503: {"title": "Reformat", "body_excerpt": "", "merge_commit_sha": b"8" * 40},
        504: {"title": "Fix crash", "body_excerpt": "", "merge_commit_sha": b"9" * 40},
    }
    monkeypatch.setattr(hb, "CodeRepo", lambda: repo)
    monkeypatch.setattr(hb, "require_code_intel", lambda: None)
    monkeypatch.setattr(hb, "schedule_code_trace", lambda **kw: None)
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(repos, "configured_repos", lambda: [_Cfg(NS, "master")])
    git: dict[str, Any] = {
        "calls": [],
        "diffs": {"8" * 40: "", "9" * 40: RENAME, "7" * 40: IN_FUNCTION},
        "error": set(),
    }

    async def _run(git_dir: str, *args: str, timeout_s: float = 20.0) -> str:
        git["calls"].append(args)
        sha = args[-3] if args[0] == "diff" else ""
        if sha in git["error"]:
            raise GitError("git diff rc=128")
        return git["diffs"].get(sha, SUBSTANTIVE)

    monkeypatch.setattr(h.gitrepo, "run", _run)

    async def _authorized(org, code_ns):
        return QA

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    holder: dict[str, Any] = {}
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])

    def use(src: _BisectSource) -> _BisectSource:
        holder["src"] = src
        return src

    return repo, git, use


def _binput(**kw: Any) -> CodeBisectCandidatesInput:
    base = {
        "org": ORG,
        "namespace": NS,
        "symbols": ["DB::MergeTreeData::loadDataParts(bool)"],
        "bad_version": "26.3",
        "good_version": "26.1",
        "limit": 4,
    }
    return CodeBisectCandidatesInput(**{**base, **kw})


@pytest.mark.asyncio
async def test_bisect_candidates_carry_the_marks_and_rank_demoted_last(wired_bisect) -> None:
    repo, git, use = wired_bisect
    use(_BisectSource(_prs()))
    out = await hb.handle_code_bisect_candidates(_binput())
    assert out.verdict == "candidates"
    by = {c["pr_number"]: c for c in out.candidates}
    # the pool is the whole scan (5), the limit (4) cuts after the re-rank: the
    # two undemoted first, then the hard-demoted by score — the whitespace-only
    # #503 (score 1.0) is the one left out, not the revert (3.0)
    assert set(by) == {500, 502, 501, 504} and out.marks["status"] == "ok"
    assert by[502]["reland_of"] == 500 and by[502]["reland_via"] == 501 and "demoted" not in by[502]
    assert by[500]["relanded_by"] == 502 and "demoted" not in by[500]
    # QA hunk header on #500 → true; the mirror diff's header on the re-land #502 → true
    assert by[500]["touches_symbol"] == "true" and by[500]["promoted"] == ["touches_symbol"]
    assert by[502]["touches_symbol"] == "true" and by[502]["promoted"] == ["touches_symbol"]
    assert by[501]["demoted"] == "revert" and by[504]["demoted"] == "rename_only"
    # #502 and #500 tie at 3.0 + 0.5; the re-land is newer
    assert [c["pr_number"] for c in out.candidates] == [502, 500, 501, 504]
    assert out.marks == {"status": "ok", "git_calls": 4, "demoted": 3, "promoted": 2, "relands": 1}
    diffs = [c for c in git["calls"] if c[0] == "diff"]
    assert diffs[0][-1] == SRC and len(diffs) == 4  # the revert is demoted by title: no diff for it
    assert repo.calls == ["records"]

    # marks off: the unmarked dicts, byte-identical, `marks` empty-shaped
    use(_BisectSource(_prs()))
    off = await hb.handle_code_bisect_candidates(_binput(marks=False))
    assert [c["pr_number"] for c in off.candidates] == [
        502,
        501,
        500,
        504,
    ]  # unmarked order: score, recency
    for c in off.candidates:
        assert not ({"demoted", "promoted", "touches_symbol", "reland_of", "relanded_by"} & set(c))
    assert set(off.candidates[0]) == {
        "pr_number",
        "title",
        "author",
        "merged_at",
        "minor_version",
        "matched_paths",
        "matched_symbols",
        "touches_affected",
        "kind",
        "has_test",
        "url",
        "score",
        "merge_commit_sha",
        "merge_sha_source",
    }
    assert off.marks == {
        "status": "skipped",
        "git_calls": 0,
        "demoted": 0,
        "promoted": 0,
        "relands": 0,
    }
    assert repo.calls == ["records"]


@pytest.mark.asyncio
async def test_bisect_marks_survive_a_ledger_outage_and_a_failed_diff(wired_bisect) -> None:
    repo, git, use = wired_bisect
    repo.fail_records = True
    use(_BisectSource(_prs()))
    out = await hb.handle_code_bisect_candidates(_binput(limit=5))
    by = {c["pr_number"]: c for c in out.candidates}
    # no bodies: the revert is still demoted by title, the re-land still found by
    # title equality with the revert's quoted title, no diff runs (no shas)
    assert out.marks["status"] == "ledger_failed" and out.marks["git_calls"] == 0
    assert (
        by[501]["demoted"] == "revert" and by[502]["reland_of"] == 500 and "demoted" not in by[500]
    )
    assert by[502]["reland_via"] == 501 and by[500]["relanded_by"] == 502
    # "Reformat" is a meta title: the soft tier, disclosed, no diff to harden it
    assert [c["pr_number"] for c in out.candidates][-1] == 501 and by[503][
        "demoted"
    ] == "meta_title"

    repo.fail_records = False
    h.reset_cache_for_tests()
    use(_BisectSource(_prs()))
    out = await hb.handle_code_bisect_candidates(_binput(limit=5))
    by = {c["pr_number"]: c for c in out.candidates}
    assert (
        by[503]["demoted"] == "whitespace_only"
        and [c["pr_number"] for c in out.candidates][-1] == 503
    )
    assert out.marks == {"status": "ok", "git_calls": 4, "demoted": 3, "promoted": 2, "relands": 1}

    git["error"].add("8" * 40)
    h.reset_cache_for_tests()
    use(_BisectSource(_prs()))
    out = await hb.handle_code_bisect_candidates(_binput(limit=5))
    by = {c["pr_number"]: c for c in out.candidates}
    # a failed diff marks nothing: #503 keeps only its soft meta tier, no whitespace_only
    assert by[503]["demoted"] == "meta_title" and by[504]["demoted"] == "rename_only"
    assert out.marks["git_calls"] == 4


def test_bisect_input_marks_defaults_on() -> None:
    assert _binput().marks is True and _binput(marks=False).marks is False
