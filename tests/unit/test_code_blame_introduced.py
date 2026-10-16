"""code.blame_range stage `introduced`: the first stage's
candidates are demoted by the ledger (backport, revert, reverted) and by a
bounded diff (whitespace-only, rename-only), promoted by the enclosing
function's record (name in title/body, review comment in span, QA diff) and
by the caller's `mentions` in a linked issue's title; a blameless range falls
back to the file-touching PRs under `introduced_unknown`; `last_touched`
keeps the first stage's order and shape."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.code.app.blame as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeBlameRangeInput
from server.code.app.index.gitrepo import GitError
from server.code.domain.index import blame_introduced as intro
from server.code.domain.index import blame_range as br
from tests.unit.test_code_blame_range import mirror_on_disk

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
PATH = "src/Storages/MergeTree/MergeTreeData.cpp"
GIT = "var/code_repos/clickhouse.git"
BASE, C1, M1, C2, TO = "b" * 40, "1" * 40, "a" * 40, "2" * 40, "e" * 40
OLD = "9" * 40
S300, S400 = "c" * 40, "d" * 40
S5, S6, S7, S8, S9 = "5" * 40, "6" * 40, "7" * 40, "8" * 40, "9" * 39 + "a"
REV_LIST = "\n".join(
    [
        f"commit {TO} {M1} {C2}",
        "1771000000\tMerge pull request #200 from ClickHouse/fix-parts",
        f"commit {C2} {M1}",
        "1770900000\tFix parts loading",
        f"commit {M1} {S300} {C1}",
        "1770000000\tMerge pull request #100 from ClickHouse/refactor",
        f"commit {C1} {S300}",
        "1769900000\tRefactor loading",
        f"commit {S300} {S400}",
        "1769800000\tTouch the file only (#300)",
        f"commit {S400} {S5}",
        "1769700000\tAdd loading of parts (#400)",
        f"commit {S5} {S6}",
        "1769600000\tRevert (#500)",
        f"commit {S6} {S7}",
        "1769500000\tBackport (#600)",
        f"commit {S7} {S8}",
        "1769400000\tRename (#700)",
        f"commit {S8} {S9}",
        "1769300000\tReformat (#800)",
        f"commit {S9} {BASE}",
        "1769200000\tFix crash (#900)",
    ]
)
SUBSTANTIVE = "@@ -1 +1 @@ void DB::MergeTreeData::other()\n-old();\n+new_thing(1);\n"
IN_FUNCTION = (
    "@@ -20 +20 @@ void DB::MergeTreeData::loadDataParts(bool skip)\n-old();\n+new_thing(1);\n"
)
RENAME = "@@ -1,2 +1,2 @@\n-auto x = load_parts(ctx);\n-return x;\n+auto y = loadParts(ctx);\n+return y;\n"


def _porcelain(lines: list[tuple[str, int, str]]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for sha, n, content in lines:
        out.append(f"{sha} {n} {n} 1")
        if sha not in seen:
            seen.add(sha)
            out += ["author X", f"author-time {1700000000 + n}", f"summary subject of {sha[:4]}"]
        out.append(f"\t{content}")
    return "\n".join(out) + "\n"


def _cand(
    n: int,
    title: str = "Change",
    *,
    lines: int = 0,
    merged: str = "2026-02-01T00:00:00+00:00",
    sha: str = "",
    evidence: str = "ledger",
) -> br.Candidate:
    from server.tracker.domain.bisect import kind_of

    return br.Candidate(
        pr_number=n,
        title=title,
        merged_at=merged,
        lines_blamed=lines,
        merge_commit_sha=sha or f"{n:040d}",
        evidence=evidence,
        kind=kind_of(title),
    )


# ---- pure helpers ---------------------------------------------------------------


def test_diff_classification_tells_whitespace_and_renames_from_behaviour() -> None:
    assert intro.classify_diff("") == "whitespace_only"
    assert intro.classify_diff(RENAME) == "rename_only"
    # the same identifier renamed two ways, or two names collapsing into one: behaviour
    assert intro.classify_diff("@@\n-a(x); b(x);\n+a(y); b(z);\n") == ""
    assert intro.classify_diff("@@\n-f(a, b);\n+f(a, a);\n") == ""
    # a keyword, a literal, an operator, an added line: behaviour
    assert intro.classify_diff("@@\n-if (x) return;\n+while (x) return;\n") == ""
    assert intro.classify_diff("@@\n-x = 1;\n+x = 2;\n") == ""
    assert intro.classify_diff("@@\n-if (a < b)\n+if (a <= b)\n") == ""
    assert intro.classify_diff("@@\n-a();\n+a();\n+b();\n") == ""
    assert intro.classify_diff(SUBSTANTIVE) == ""
    # header lines never count as content
    assert (
        intro.classify_diff("diff --git a/x b/x\nindex 1..2\n--- a/x\n+++ b/x\n")
        == "whitespace_only"
    )


def test_diff_hunk_headers_say_whether_the_function_was_touched() -> None:
    d = (
        "@@ -661 +661 @@ void DistributedSink::onFinish()\n-a();\n+b();\n"
        "@@ -705 +705 @@ void DistributedSink::onCancel() noexcept\n-c();\n+d();\n"
    )
    assert intro.diff_touches(d, ["onCancel"]) is True
    assert intro.diff_touches(d, ["loadDataParts"]) is False
    assert intro.diff_touches(d, []) is None and intro.diff_touches("", ["x"]) is None
    assert intro.diff_touches("@@ -1 +1 @@\n-a\n+b\n", ["x"]) is False  # header without context


def test_ledger_demotion_rules_and_behaviour_words() -> None:
    assert intro.is_derivative("Backport #99 to 26.1: Fix x")
    assert intro.is_derivative("Cherry pick #99 to 26.1") and intro.is_derivative("[Sync] private")
    assert not intro.is_derivative("Fix backport script")
    assert intro.is_revert('Revert "Fix x"') and intro.is_revert("Undo", "This reverts commit abc")
    assert intro.is_revert("Undo x", "Reverts ClickHouse/ClickHouse#100")
    assert not intro.is_revert("Fix parts", "nothing here")
    # a revert of a revert re-lands the change: not a revert, even with the body's link
    assert not intro.is_revert(
        'Revert "Revert the PromQL topk plan"', "Reverts ClickHouse/ClickHouse#114326"
    )
    assert intro.is_revert('Revert "Fix x"', "Reverts ClickHouse/ClickHouse#1")
    assert intro.reverted_prs(
        "Reverts ClickHouse/ClickHouse#100\nreverted https://x/pull/7 too"
    ) == {100, 7}
    words = intro.behaviour_words(
        "clickhouse-local prints TSV instead of PrettyCompact in interactive mode"
    )
    assert words == ["clickhouse", "local", "prints", "prettycompact", "interactive", "mode"] or (
        "prettycompact" in words and "instead" not in words and "tsv" not in words
    )
    assert intro.words_in("Interactive clickhouse-local shows TSV, not PrettyCompact", words) == [
        w for w in words if w in ("clickhouse", "local", "prettycompact", "interactive")
    ]
    assert intro.words_in("", words) == [] and intro.words_in("x", []) == []


def test_enclosing_symbol_is_the_innermost_span_at_the_midpoint() -> None:
    rows = [
        {"qualified_name": "DB::MergeTreeData", "start_line": 1, "end_line": 5000},
        {
            "qualified_name": "DB::MergeTreeData::loadDataParts",
            "start_line": 2857,
            "end_line": 3263,
        },
        {"qualified_name": "lambda", "start_line": 2900, "end_line": 2905},
        {"qualified_name": "DB::MergeTreeData::other", "start_line": 3300, "end_line": 3400},
    ]
    assert (
        intro.enclosing_symbol(rows, 2890, 2910)["qualified_name"]
        == "DB::MergeTreeData::loadDataParts"
    )
    assert intro.enclosing_symbol(rows, 2900, 2904)["qualified_name"] == "lambda"
    # the midpoint falls between functions: the span with the most lines in common
    assert (
        intro.enclosing_symbol(rows[1:], 3250, 3320)["qualified_name"] == "DB::MergeTreeData::other"
    )
    assert intro.enclosing_symbol(rows[1:], 6000, 6010) is None
    assert intro.enclosing_symbol([], 1, 2) is None


def test_marks_and_introduced_order() -> None:
    cands = [
        _cand(
            100, "Refactor loading", lines=2, merged="2026-02-01T00:00:00+00:00", evidence="both"
        ),
        _cand(
            200, "Fix parts loading", lines=1, merged="2026-02-10T00:00:00+00:00", evidence="blame"
        ),
        _cand(300, 'Revert "Add loading of parts"', merged="2026-02-05T00:00:00+00:00"),
        _cand(400, "Add loading of parts", merged="2026-01-20T00:00:00+00:00"),
        _cand(500, "Backport #123 to 26.2: Fix y", merged="2026-02-12T00:00:00+00:00"),
        _cand(600, "Rename a helper", merged="2026-02-11T00:00:00+00:00"),
        _cand(700, "Touch only", merged="2026-02-09T00:00:00+00:00"),
        _cand(800, "Make loadDataParts skip broken parts", merged="2026-01-25T00:00:00+00:00"),
    ]
    marks = intro.mark_candidates(
        cands,
        bodies={300: "Reverts ClickHouse/ClickHouse#400", 800: "touches loadDataParts"},
        linked={700: [555], 800: [556]},
        issue_titles={555: "Parts loading crashes on restart"},
        words=intro.behaviour_words("crash when loading parts on restart"),
        frames=["loadDataParts"],
        span_comment_prs={200},
        tracker_names={},
        qa_ok=False,
        diff_kinds={600: "rename_only"},
    )
    assert [marks[n].last_touched_rank for n in (100, 200, 300)] == [1, 2, 3]
    assert marks[100].demoted == "" and marks[100].meta and marks[100].touches_symbol == "unknown"
    # "Fix parts loading" carries two of the behaviour words itself
    assert marks[200].promoted == ["review_comment_in_span", "title_names_behaviour"]
    assert marks[200].touches_symbol == "named" and marks[200].behaviour_words == [
        "loading",
        "parts",
    ]
    assert marks[300].demoted == "revert" and marks[400].demoted == "reverted"
    assert marks[500].demoted == "backport" and marks[600].demoted == "rename_only"
    assert marks[700].promoted == ["issue_names_behaviour"]
    assert marks[700].behaviour_words == ["loading", "parts", "restart"]  # "crashes" is not "crash"
    assert marks[800].promoted == ["names_symbol", "title_names_behaviour"]
    assert marks[800].touches_symbol == "named"
    order = [c.pr_number for c in intro.rank_introduced(cands, marks, blameless=False)]
    # behaviour issue first; then undemoted change-titled: blamed (#200, named) before
    # ledger-only with symbol evidence (#800); then the meta-titled blamed #100; then
    # the demoted, newest first
    # (the demoted keep the same tiers among themselves: the revert and the
    # reverted PR name the behaviour words, the backport does not, the rename is meta)
    assert order == [700, 200, 800, 100, 300, 400, 500, 600]
    blameless = [c.pr_number for c in intro.rank_introduced(cands, marks, blameless=True)]
    assert blameless == [700, 200, 800, 100, 300, 400, 500, 600]
    # a candidate no mark knows keeps a neutral key
    assert [
        c.pr_number for c in intro.rank_introduced([_cand(1), _cand(2)], {}, blameless=True)
    ] == [2, 1]
    # meta tier: a change-titled ledger-only PR outranks the meta-titled blamed one
    d = {c.pr_number: c.as_dict() for c in cands}
    assert "demoted" not in d[200] and d[100].get("demoted") is None  # marks not attached yet
    for c in cands:
        c.mark = marks[c.pr_number]
    d = {c.pr_number: c.as_dict() for c in cands}
    assert d[100]["demoted"] == "meta_title" and d[300]["demoted"] == "revert"
    assert d[700]["promoted"] == ["issue_names_behaviour"] and d[700]["last_touched_rank"] == 7
    assert d[200]["touches_symbol"] == "named" and "touches_symbol" not in d[100]


def test_tracker_diff_settles_symbol_touch_when_bound() -> None:
    cands = [_cand(1, "A", lines=1), _cand(2, "B"), _cand(3, "C", lines=0)]
    marks = intro.mark_candidates(
        cands,
        bodies={},
        linked={},
        issue_titles={},
        words=[],
        frames=["loadDataParts"],
        span_comment_prs=set(),
        tracker_names={1: ["loadDataParts"], 2: [], 3: []},
        qa_ok=True,
        diff_kinds={},
    )
    assert marks[1].touches_symbol == "true" and marks[1].promoted == ["touches_symbol"]
    # a ledger-only PR whose diff does not name the function ranks after the
    # evidence but is never demoted for it (a `false` here is wrong on 12 of
    # 32 blamed golds — a change inside a body need not carry the function's
    # name)
    assert marks[2].touches_symbol == "false" and marks[2].demoted == ""
    assert marks[3].touches_symbol == "false" and marks[3].demoted == ""
    # the git hunk header outranks a QA miss: the header names the enclosing
    # function positionally, as of the PR's own commit
    header = intro.mark_candidates(
        [_cand(2, "B"), _cand(4, "D")],
        bodies={},
        linked={},
        issue_titles={},
        words=[],
        frames=["loadDataParts"],
        span_comment_prs=set(),
        tracker_names={2: [], 4: []},
        qa_ok=True,
        diff_kinds={},
        diff_touch={2: True, 4: False},
    )
    assert header[2].touches_symbol == "true" and header[2].promoted == ["touches_symbol"]
    assert header[4].touches_symbol == "false" and header[4].demoted == ""
    blamed = intro.mark_candidates(
        [_cand(2, "B", lines=3)],
        bodies={},
        linked={},
        issue_titles={},
        words=[],
        frames=["loadDataParts"],
        span_comment_prs=set(),
        tracker_names={2: []},
        qa_ok=True,
        diff_kinds={},
    )
    assert (
        blamed[2].touches_symbol == "false" and blamed[2].demoted == ""
    )  # blame says it did touch


def test_diff_order_and_blameless_rule() -> None:
    cands = [
        _cand(n, "x", lines=(1 if n % 2 else 0), merged=f"2026-01-{n:02d}T00:00:00+00:00")
        for n in range(1, 16)
    ]
    cands.append(br.Candidate(pr_number=99, title="no sha", lines_blamed=5))
    marks = {c.pr_number: intro.Mark() for c in cands}
    marks[3].demoted = "backport"
    order = [c.pr_number for c in intro.diff_order(cands, marks)]
    assert len(order) == intro.MAX_DIFF_CALLS and 3 not in order and 99 not in order
    assert order[:7] == [15, 13, 11, 9, 7, 5, 1] and order[7:] == [14, 12, 10]
    assert intro.is_blameless(blame_ok=False, attributed_lines=3, noise_only=False)
    assert intro.is_blameless(blame_ok=True, attributed_lines=0, noise_only=False)
    assert intro.is_blameless(blame_ok=True, attributed_lines=2, noise_only=True)
    assert not intro.is_blameless(blame_ok=True, attributed_lines=2, noise_only=False)


# ---- handler ----------------------------------------------------------------------


class _Cfg:
    def __init__(self, namespace: str, branch: str) -> None:
        self.org, self.namespace, self.git_branch = ORG, namespace, branch
        self.git_dir, self.repo = GIT, "ClickHouse/ClickHouse"
        self.git_remote = "https://github.com/ClickHouse/ClickHouse.git"


class _Repo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.functions: list[dict[str, Any]] = [
            {
                "symbol_id": "s1",
                "qualified_name": "DB::MergeTreeData",
                "start_line": 1,
                "end_line": 5000,
            },
            {
                "symbol_id": "s2",
                "qualified_name": "DB::MergeTreeData::loadDataParts",
                "start_line": 5,
                "end_line": 40,
            },
        ]
        self.comments: list[dict[str, Any]] = []
        self.items: dict[int, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_functions = False

    async def all_state(self, org: str, ns: str) -> dict[str, dict[str, Any]]:
        return {"git_head": {"value": TO}}

    async def pr_history_in_window(self, org, ns, path, *, since, until, limit=200, branch=""):
        self.calls.append("ledger")
        return list(self.rows)

    async def live_paths_present(self, org, ns, paths):
        return set(paths)

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        self.calls.append("functions")
        if self.fail_functions:
            raise RuntimeError("ch down")
        return list(self.functions)

    async def review_comments_for(self, org, ns, paths, *, limit=30, humans_only=True):
        self.calls.append("comments")
        return list(self.comments)

    async def items_by_number(self, org, ns, numbers):
        self.calls.append(f"items:{','.join(str(n) for n in numbers)}")
        return {n: self.items[n] for n in numbers if n in self.items}


class _Git:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.blame_text = _porcelain([(C1, 10, "a();"), (C1, 11, "b();"), (C2, 12, "c();")])
        self.blame_error = ""
        self.diffs: dict[str, str] = {}
        self.diff_error: set[str] = set()

    async def __call__(self, git_dir: str, *args: str, timeout_s: float = 20.0) -> str:
        self.calls.append(args)
        cmd = args[0]
        if cmd == "for-each-ref":
            if "refs/tags/v*.1.1-new" in args:
                return f"v26.2.1.1-new {BASE} 1769452776\nv26.3.1.1-new {'3' * 40} 1771937490\n"
            if any("26.2" in a for a in args):
                return BASE + "\n"
            return TO + "\n" if any("26.3" in a for a in args) else ""
        if cmd == "rev-parse":
            return args[-1].split("^")[0] + "\n"
        if cmd == "merge-base":
            return BASE + "\n"
        if cmd == "log":
            times = {BASE: 1769000000, TO: 1771000000}
            return "".join(f"{sha} {times[sha]}\n" for sha in args[3:] if sha in times)
        if cmd == "rev-list":
            return REV_LIST + "\n"
        if cmd == "blame":
            if self.blame_error:
                raise GitError(self.blame_error)
            return self.blame_text
        if cmd == "diff":
            sha = args[-3]
            if sha in self.diff_error:
                raise GitError("git diff rc=128: bad object")
            return self.diffs.get(sha, SUBSTANTIVE)
        raise AssertionError(f"unexpected git {args}")


def _row(n: int, title: str, sha: str, merged: datetime, **extra: Any) -> dict[str, Any]:
    return {
        "pr_number": n,
        "title": title,
        "author": "a",
        "merged_at": merged,
        "merge_commit_sha": sha.encode(),
        **extra,
    }


@pytest.fixture
def wired(monkeypatch, tmp_path):
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
    git.diffs[S400] = IN_FUNCTION  # #400's hunks land inside loadDataParts
    repo.rows = [
        _row(100, "Refactor loading", M1, datetime(2026, 2, 1), body_excerpt="moves code around"),
        _row(300, "Touch the file only", S300, datetime(2026, 1, 25), linked_issues=[555]),
        _row(
            400,
            "Add loading of parts",
            S400,
            datetime(2026, 1, 20),
            body_excerpt="loadDataParts learns to load parts",
        ),
    ]
    return repo, git


def _input(**kw: Any) -> CodeBlameRangeInput:
    base = {
        "org": ORG,
        "namespace": NS,
        "path": PATH,
        "start_line": 10,
        "end_line": 12,
        "from_version": "26.2",
        "to_version": "26.3",
    }
    return CodeBlameRangeInput(**{**base, **kw})


@pytest.mark.asyncio
async def test_introduced_is_the_default_and_reranks_below_the_first_stage(wired) -> None:
    repo, git = wired
    out = await h.handle_code_blame_range(_input())
    assert out.stage == "introduced" and out.verdict == "candidates" and out.epistemic == "exact"
    assert out.symbol == {
        "qualified_name": "DB::MergeTreeData::loadDataParts",
        "start_line": 5,
        "end_line": 40,
    }
    # first stage: #100 (2 lines) > #200 (1 line) > ledger-only #300, #400 by recency
    assert [(r["pr_number"], r["rank"]) for r in out.last_touched] == [
        (100, 1),
        (200, 2),
        (300, 3),
        (400, 4),
    ]
    # introduced: the blamed fix #200 first; the ledger-only #400 names the
    # function; #300 plain; the meta-titled refactor #100 last of the undemoted
    assert [c["pr_number"] for c in out.candidates] == [200, 400, 300, 100]
    by = {c["pr_number"]: c for c in out.candidates}
    assert by[100]["demoted"] == "meta_title" and by[100]["last_touched_rank"] == 1
    # #400: its hunk header names the function AND its body does; blamed PRs
    # are `true` by blame without a promotion
    assert by[400]["promoted"] == ["touches_symbol", "names_symbol"]
    assert by[400]["touches_symbol"] == "true" and by[200]["touches_symbol"] == "true"
    assert by[300]["touches_symbol"] == "false" and "promoted" not in by[300]
    assert "demoted" not in by[200] and by[200]["last_touched_rank"] == 2
    intro_leg = out.legs["introduced"]
    assert (
        intro_leg["status"] == "ok" and intro_leg["git_calls"] == 4 and not intro_leg["blameless"]
    )
    assert intro_leg["demoted"] == 0 and intro_leg["promoted"] == 1
    diffs = [c for c in git.calls if c[0] == "diff"]
    assert diffs[0] == (
        "diff",
        "-w",
        "-U0",
        "--no-color",
        f"{TO}^1",
        TO,
        "--",
        PATH,
    )  # newest blamed first
    assert {c[-3] for c in diffs} == {M1, TO, S300, S400}
    assert "Stage `introduced`" in out.note and "LAST touched" in out.note
    assert repo.calls.count("functions") == 1 and repo.calls.count("comments") == 1
    assert not [c for c in repo.calls if c.startswith("items")]  # no mentions: no issue read

    # warm: diffs served from the cache
    out2 = await h.handle_code_blame_range(_input())
    assert out2.legs["introduced"]["git_calls"] == 0 and out2.candidates == out.candidates


@pytest.mark.asyncio
async def test_ledger_and_diff_demotions_and_the_mentions_promotion(wired) -> None:
    repo, git = wired
    repo.rows += [
        _row(
            500,
            'Revert "Add loading of parts"',
            "5" * 40,
            datetime(2026, 2, 15),
            body_excerpt="Reverts ClickHouse/ClickHouse#400",
        ),
        _row(600, "Backport #123 to 26.3: Fix y", "6" * 40, datetime(2026, 2, 16)),
        _row(700, "Rename a helper", "7" * 40, datetime(2026, 2, 17)),
        _row(800, "Reformat", "8" * 40, datetime(2026, 2, 18)),
        _row(
            900, "Fix crash on restart", "9" * 39 + "a", datetime(2026, 2, 19), linked_issues=[777]
        ),
    ]
    git.diffs["7" * 40] = RENAME
    git.diffs["8" * 40] = ""
    git.diff_error.add("9" * 39 + "a")
    git.diffs[S300] = IN_FUNCTION
    repo.items = {555: {"title": "Parts loading crashes on restart"}, 777: {"title": "Unrelated"}}
    repo.comments = [
        {
            "pr_number": 300,
            "line": 11,
            "author": "r",
            "body": "x" * 40,
            "comment_created_at": datetime(2026, 1, 26),
        }
    ]
    out = await h.handle_code_blame_range(
        _input(mentions="server crashes loading parts after restart")
    )
    by = {c["pr_number"]: c for c in out.candidates}
    # behaviour tier: the issue hit #300, then the title hits (#200 blamed, #900 not);
    # then the undemoted #100 (meta); then the demoted — #400 (reverted) keeps its
    # symbol evidence, the rest by title tier and recency
    assert [c["pr_number"] for c in out.candidates] == [300, 200, 900, 100, 400, 500, 600, 800, 700]
    assert by[300]["promoted"] == [
        "review_comment_in_span",
        "touches_symbol",
        "issue_names_behaviour",
    ]
    assert by[300]["behaviour_words"] == ["crashes", "loading", "parts", "restart"]
    assert by[300]["touches_symbol"] == "true" and by[900]["promoted"] == ["title_names_behaviour"]
    assert "touches_symbol" not in by[900]  # its diff failed: nothing learned
    assert by[500]["demoted"] == "revert" and by[400]["demoted"] == "reverted"
    assert by[600]["demoted"] == "backport" and by[700]["demoted"] == "rename_only"
    assert (
        by[800]["demoted"] == "whitespace_only" and "demoted" not in by[900]
    )  # diff failed: no demotion
    leg = out.legs["introduced"]
    assert leg["demoted"] == 5 and leg["git_calls"] == 6  # blamed 200, 100; then 900, 800, 700, 300
    assert [c for c in repo.calls if c.startswith("items")] == ["items:555,777"]
    # the response's limit cuts after the re-rank: a promoted PR past `limit` in
    # first-stage order still surfaces
    small = await h.handle_code_blame_range(_input(mentions="crashes loading parts", limit=2))
    assert [c["pr_number"] for c in small.candidates] == [300, 200]
    assert len(small.last_touched) == 9


@pytest.mark.asyncio
async def test_blameless_range_falls_back_to_the_file_record(wired) -> None:
    repo, git = wired
    git.blame_text = _porcelain([(OLD, 10, "a();"), (OLD, 11, "b();"), (OLD, 12, "c();")])
    out = await h.handle_code_blame_range(_input())
    assert out.verdict == "introduced_unknown" and out.epistemic == "lower_bound"
    assert (
        out.causes == []
        and out.lines["outside_window"] == 3
        and out.legs["introduced"]["blameless"]
    )
    # named before unnamed, then recency: #400 names the function; #300 newer than #100? no —
    # #100 is meta-titled, so the change-titled #300 precedes it
    assert [c["pr_number"] for c in out.candidates] == [400, 300, 100]
    assert [r["pr_number"] for r in out.last_touched] == [100, 300, 400]

    # every in-window line is noise: the first stage still answers, the second
    # says unknown but keeps blame's pick first (the blamed #200 before the
    # ledger-only #400 / #300; the meta-titled blamed #100 after them)
    git.blame_text = _porcelain([(C1, 10, "}"), (C2, 11, "// c")])
    h.reset_cache_for_tests()
    out = await h.handle_code_blame_range(_input())
    assert out.verdict == "introduced_unknown" and out.lines["noise_only"]
    assert [c["pr_number"] for c in out.candidates] == [200, 400, 300, 100]

    # the blame leg failed: the ledger floor, blame_failed named, introduced unknown
    git.blame_error = "git blame timed out after 5.0s"
    h.reset_cache_for_tests()
    out = await h.handle_code_blame_range(_input())
    assert out.verdict == "introduced_unknown" and out.causes == ["blame_failed"]
    assert [c["pr_number"] for c in out.candidates] == [400, 300, 100]

    # the functions read failed: no symbol, the stage still runs on the ledger
    repo.fail_functions = True
    git.blame_error = ""
    h.reset_cache_for_tests()
    out = await h.handle_code_blame_range(_input())
    assert out.symbol == {} and out.legs["introduced"]["status"] == "ok"
    assert [c["pr_number"] for c in out.candidates] == [200, 300, 400, 100]


@pytest.mark.asyncio
async def test_last_touched_stage_keeps_the_first_stage_shape(wired) -> None:
    repo, git = wired
    out = await h.handle_code_blame_range(_input(stage="last_touched"))
    assert out.stage == "last_touched" and out.verdict == "candidates"
    assert [c["pr_number"] for c in out.candidates] == [100, 200, 300, 400]
    for c in out.candidates:
        # `lines_in_range` / `commits` ride a blamed
        # candidate (the stub repo has no review read, so no `reviews`)
        blamed = {"lines_in_range", "commits"} if c["lines_blamed"] else set()
        assert (
            set(c)
            == {
                "pr_number",
                "title",
                "author",
                "merged_at",
                "first_release",
                "lines_blamed",
                "share_of_range",
                "touches_file",
                "evidence",
                "kind",
                "merge_commit_sha",
                "merge_sha_source",
                "url",
            }
            | blamed
        )
    assert [(r["pr_number"], r["rank"]) for r in out.last_touched] == [
        (100, 1),
        (200, 2),
        (300, 3),
        (400, 4),
    ]
    assert out.symbol == {} and out.legs["introduced"] == {
        "status": "skipped",
        "ms": 0,
        "git_calls": 0,
        "demoted": 0,
        "promoted": 0,
        "blameless": False,
    }
    assert "Stage `introduced`" not in out.note
    assert "functions" not in repo.calls and not [c for c in git.calls if c[0] == "diff"]
    # the window not resolving is named before either stage runs
    bad = await h.handle_code_blame_range(_input(to_version="27.9"))
    assert bad.verdict == "unknown" and bad.stage == "introduced" and bad.last_touched == []


def test_input_validates_stage_and_mentions() -> None:
    assert _input().stage == "introduced" and _input().mentions == ""
    with pytest.raises(ValueError):
        _input(stage="guess")
    with pytest.raises(ValueError):
        _input(mentions="x" * 301)
