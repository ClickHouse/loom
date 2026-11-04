"""code.blame_range: a line range names the merged PRs that
last touched it inside a release window — blame lines fold into PRs through
the window's merge graph under refactoring-aware line rules (blame the lines a
fix deleted), the ledger leg is the file-level floor, the two legs merge into
one ranked list, the verdict names what was missing, and the QA half is
optional."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import server.code.app.blame as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeBlameRangeInput
from server.code.app.index.gitrepo import GitError
from server.code.domain.index import blame_range as br
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
PATH = "src/Storages/MergeTree/MergeTreeData.cpp"
GIT = "var/code_repos/clickhouse.git"

# a window of five commits: TO is the merge of PR #200 (second parent C2 on a
# branch), whose first parent M1 is the merge of PR #100 (second parent C1),
# whose first parent is BASE (outside the range)
BASE, C1, M1, C2, TO = "b" * 40, "1" * 40, "a" * 40, "2" * 40, "e" * 40
OLD = "9" * 40  # a commit older than BASE
REV_LIST = "\n".join(
    [
        f"commit {TO} {M1} {C2}",
        "1771000000\tMerge pull request #200 from ClickHouse/fix-parts",
        f"commit {C2} {M1}",
        "1770900000\tFix parts loading",
        f"commit {M1} {BASE} {C1}",
        "1770000000\tMerge pull request #100 from ClickHouse/refactor",
        f"commit {C1} {BASE}",
        "1769900000\tRefactor loading",
    ]
)


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


# ---- pure helpers -------------------------------------------------------------


def test_refs_classify_and_ranges_clamp() -> None:
    assert br.classify_ref("26.3") == ("minor", "26.3") and br.classify_ref("v26.03") == (
        "minor",
        "26.3",
    )
    assert br.classify_ref("v26.3.4.12-lts") == ("full", "26.3.4.12")
    assert br.classify_ref("ABCDEF1") == ("sha", "abcdef1") and br.classify_ref("26.13") == ("", "")
    assert br.clamp_range(10, 12) == (10, 12, False)
    assert br.clamp_range(1, 1000) == (1, 400, True) and br.clamp_range(0, -5) == (1, 1, False)


def test_branch_points_and_first_release() -> None:
    points = br.parse_branch_points(
        "v26.3.1.1-new " + "3" * 40 + " 1771937490\nv26.2.1.1-new " + "2" * 40 + " 1769452776\n"
        "v26.3.2.1-lts x 1\nbroken line\n"
    )
    assert [p[0] for p in points] == ["26.2", "26.3"]
    # v26.3.1.1-new (Feb 24) cuts branch 26.2: a PR merged before it ships in 26.2
    merged = datetime.fromtimestamp(1770000000, tz=UTC)
    assert br.first_release_of(merged, "Fix x", points) == "26.2"
    # before the oldest known cut: unknown; after the newest: the minor master is on
    assert br.first_release_of(datetime.fromtimestamp(1769000000, tz=UTC), "Fix x", points) == ""
    assert (
        br.first_release_of(datetime.fromtimestamp(1780000000, tz=UTC), "Fix x", points) == "26.3"
    )
    assert br.first_release_of(merged, "Fix x", []) == ""
    assert br.first_release_of(merged, "Backport #99 to 26.1: Fix x", points) == "26.1"
    assert br.first_release_of(None, "Fix", points) == ""


def test_porcelain_parses_and_noise_rules_follow_the_extension() -> None:
    lines = br.parse_porcelain(
        _porcelain([(C1, 10, "int x = 1;"), (C1, 11, "  // note"), (C2, 12, "}")])
    )
    assert [(b.sha, b.line, b.content) for b in lines] == [
        (C1, 10, "int x = 1;"),
        (C1, 11, "  // note"),
        (C2, 12, "}"),
    ]
    assert lines[0].summary == f"subject of {C1[:4]}" and lines[2].author_time == 1700000012
    assert br.is_noise_line("  // note", "a.cpp") and br.is_noise_line("}", "a.cpp")
    assert br.is_noise_line("   ", "a.cpp") and br.is_noise_line("* continued", "a.h")
    assert not br.is_noise_line("# not a comment in C", "a.cpp")
    assert br.is_noise_line("# comment", "a.py") and not br.is_noise_line("x = 1  # c", "a.py")
    assert br.is_noise_line("-- sql comment", "q.sql") and not br.is_noise_line("SELECT 1", "q.sql")


def test_graph_chain_and_merge_mapping() -> None:
    g = br.parse_graph(REV_LIST, TO)
    assert g.chain == [TO, M1] and g.commits == {TO, C2, M1, C1} and not g.truncated
    assert g.times[TO] == 1771000000 and g.subjects[C1] == "Refactor loading"
    bare = br.parse_graph(f"commit {TO} {M1}\nno time here\n", TO)
    assert bare.subjects[TO] == "no time here" and bare.times == {}
    children = br.children_of(g)
    assert br.merge_of(C1, g, children) == M1 and br.merge_of(C2, g, children) == TO
    assert br.merge_of(TO, g, children) == TO and br.merge_of(OLD, g, children) == ""
    assert br.pr_number_of("Merge pull request #200 from x") == 200
    assert br.pr_number_of("Fix parts loading (#300)") == 300 and br.pr_number_of("plain") == 0


def test_attribution_applies_the_deleted_line_rules() -> None:
    g = br.parse_graph(REV_LIST, TO)
    blame = br.parse_porcelain(
        _porcelain(
            [
                (C1, 10, "a();"),
                (C1, 11, "b();"),
                (C2, 12, "c();"),
                (C2, 13, "}"),  # format-only: dropped
                (OLD, 14, "d();"),  # before the window
                (C1, 15, "// comment"),  # comment: dropped
            ]
        )
    )
    att = br.attribute(blame, g, PATH)
    assert att.lines == {100: 2, 200: 1} and att.merge_sha == {100: M1, 200: TO}
    assert att.as_counts() == {
        "total": 6,
        "attributed": 3,
        "noise": 2,
        "outside_window": 1,
        "unmapped": 0,
        "noise_only": False,
    }
    # a range that is all braces and comments still has a "last touched": the
    # noise lines are attributed after all, flagged noise_only
    only = br.attribute(
        br.parse_porcelain(_porcelain([(C2, 13, "}"), (C1, 15, "// c"), (OLD, 16, "")])), g, PATH
    )
    assert only.lines == {200: 1, 100: 1} and only.noise_only and only.noise == 3
    assert only.as_counts()["outside_window"] == 1
    # noise lines that are all older than the window stay outside; nothing is attributed
    older = br.attribute(br.parse_porcelain(_porcelain([(OLD, 13, "}"), (OLD, 14, "")])), g, PATH)
    assert older.lines == {} and not older.noise_only and older.noise == 2
    assert att.subject[200].startswith("Merge pull request #200")
    # a merge whose subject carries no number falls back to the ledger's sha map
    g2 = br.parse_graph(
        REV_LIST.replace("Merge pull request #200 from ClickHouse/fix-parts", "merge"), TO
    )
    att2 = br.attribute(blame, g2, PATH, {TO: 200})
    assert att2.lines[200] == 1
    att3 = br.attribute(blame, g2, PATH, {})
    assert att3.unmapped == 1 and 200 not in att3.lines


def test_merge_legs_ranks_blame_lines_then_recency_then_ledger_only() -> None:
    g = br.parse_graph(REV_LIST, TO)
    blame = br.parse_porcelain(_porcelain([(C1, 10, "a();"), (C1, 11, "b();"), (C2, 12, "c();")]))
    att = br.attribute(blame, g, PATH)
    rows = [
        {
            "pr_number": 100,
            "title": "Refactor loading",
            "author": "a",
            "merged_at": datetime(2026, 2, 1),
            "merge_commit_sha": M1.encode(),
        },
        {
            "pr_number": 300,
            "title": "Touch the file only",
            "author": "c",
            "merged_at": datetime(2026, 2, 20),
            "merge_commit_sha": ("c" * 40).encode(),
        },
    ]
    points = [("26.3", int(datetime(2026, 2, 24, tzinfo=UTC).timestamp()), "3" * 40)]
    out = br.merge_legs(att, rows, range_len=3, branch_points=points, repo="ClickHouse/ClickHouse")
    assert [(c.pr_number, c.lines_blamed, c.evidence) for c in out] == [
        (100, 2, "both"),
        (200, 1, "blame"),
        (300, 0, "ledger"),
    ]
    top = out[0].as_dict()
    assert top["share_of_range"] == 0.667 and top["touches_file"] is True and top["kind"] == "meta"
    assert top["first_release"] == "" and top["url"].endswith("/pull/100")  # one cut known: unknown
    assert top["merge_commit_sha"] == M1 and "has_test" not in top
    assert out[1].title == "Merge pull request #200 from ClickHouse/fix-parts"
    assert out[1].merged_at == "2026-02-13T16:26:40+00:00"  # the merge's committer time
    assert out[2].as_dict()["evidence"] == "ledger" and out[2].share_of_range == 0.0


def test_verdicts_name_what_was_missing() -> None:
    c = [br.Candidate(pr_number=1, lines_blamed=1)]
    assert br.verdict_of(
        candidates=[], window_ok=False, blame_ok=True, ledger_ok=True, path_indexed=True
    ) == ("unknown", ["window_unresolved"])
    assert br.verdict_of(
        candidates=c, window_ok=True, blame_ok=True, ledger_ok=True, path_indexed=True
    ) == ("candidates", [])
    assert br.verdict_of(
        candidates=[], window_ok=True, blame_ok=True, ledger_ok=True, path_indexed=True
    ) == ("no_candidates", [])
    assert br.verdict_of(
        candidates=[], window_ok=True, blame_ok=True, ledger_ok=True, path_indexed=False
    ) == ("unknown", ["path_not_indexed"])
    assert br.verdict_of(
        candidates=[],
        window_ok=True,
        blame_ok=False,
        ledger_ok=True,
        path_indexed=True,
        blame_error="timeout",
    ) == ("unknown", ["blame_failed"])
    assert br.verdict_of(
        candidates=[],
        window_ok=True,
        blame_ok=False,
        ledger_ok=True,
        path_indexed=True,
        blame_error="no_such_path",
    ) == ("unknown", ["path_not_indexed"])
    # the ledger answers while blame failed: candidates, lower_bound, cause kept
    assert br.verdict_of(
        candidates=c, window_ok=True, blame_ok=False, ledger_ok=True, path_indexed=True
    ) == ("candidates", ["blame_failed"])
    assert br.epistemic_of(True) == "exact" and br.epistemic_of(False) == "lower_bound"


# ---- handler -----------------------------------------------------------------


class _Cfg:
    def __init__(self, namespace: str, branch: str) -> None:
        self.org, self.namespace, self.git_branch = ORG, namespace, branch
        self.git_dir, self.repo = GIT, "ClickHouse/ClickHouse"
        self.git_remote = "https://github.com/ClickHouse/ClickHouse.git"
        self.label, self.source = f"{ORG}/{namespace}", "config"


class _Repo:
    def __init__(self) -> None:
        self.state: dict[str, str] = {NS: TO, "code-clickhouse-26-3": TO}
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.window_calls: list[tuple[str, str, datetime, datetime]] = []
        self.indexed = {PATH}
        self.fail_ledger = False

    async def all_state(self, org: str, ns: str) -> dict[str, dict[str, Any]]:
        head = self.state.get(ns, "")
        return {"git_head": {"value": head}} if head else {}

    async def pr_history_in_window(self, org, ns, path, *, since, until, limit=200, branch=""):
        self.window_calls.append((ns, path, since, until))
        if self.fail_ledger:
            raise RuntimeError("ch down")
        return list(self.rows.get(ns, []))

    async def live_paths_present(self, org, ns, paths):
        return {p for p in paths if p in self.indexed}

    # the introduced-stage reads; the first-stage tests below pin stage="last_touched",
    # so these answer nothing here (tests/unit/test_code_blame_introduced.py
    # exercises them)
    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return []

    async def review_comments_for(self, org, ns, paths, *, limit=30, humans_only=True):
        return []

    async def items_by_number(self, org, ns, numbers):
        return {}


class _Git:
    """A stubbed git: for-each-ref, rev-parse, merge-base, log --no-walk,
    rev-list --parents, blame --porcelain — recorded, scriptable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.blame_text = _porcelain([(C1, 10, "a();"), (C1, 11, "b();"), (C2, 12, "c();")])
        self.blame_error = ""
        self.times = {BASE: 1769000000, TO: 1771000000}

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
            return "".join(f"{sha} {self.times[sha]}\n" for sha in args[3:] if sha in self.times)
        if cmd == "rev-list":
            return REV_LIST + "\n"
        if cmd == "blame":
            if self.blame_error:
                raise GitError(self.blame_error)
            return self.blame_text
        if cmd == "diff":
            return "@@ -1 +1 @@\n-old();\n+new_thing(1);\n"
        raise AssertionError(f"unexpected git {args}")


def mirror_on_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The fake cfgs' mirror is present on this host, so no clone is started."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / GIT).mkdir(parents=True)

    from server.code.app.index import mirrors

    async def _holds(cfg: Any, want: str, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(mirrors, "holds", _holds)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    h.reset_cache_for_tests()
    mirror_on_disk(monkeypatch, tmp_path)
    repo, git = _Repo(), _Git()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(h.gitrepo, "run", git)
    cfgs = [_Cfg(NS, "master"), _Cfg("code-clickhouse-26-3", "26.3")]
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(repos, "configured_repos", lambda: cfgs)
    monkeypatch.setattr(h.settings, "code_git_branch", "master")
    state: dict[str, Any] = {"qa": "", "src": None}

    async def _authorized(org, code_ns, *, hides_answer=True):
        return state["qa"]

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: state["src"])
    return repo, git, state


def test_op_is_registered_experimental_viewer_read_and_input_is_checked() -> None:
    op = OPERATIONS_BY_NAME["code.blame_range"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    with pytest.raises(ValueError):
        CodeBlameRangeInput(org=ORG, namespace=NS, path=PATH, start_line=10, end_line=5)
    with pytest.raises(ValueError):
        CodeBlameRangeInput(
            org=ORG, namespace=NS, path=PATH, start_line=1, end_line=5, to_version="26.3", to_sha=TO
        )


@pytest.mark.asyncio
async def test_both_legs_merge_into_ranked_candidates(wired) -> None:
    repo, git, _ = wired
    repo.rows[NS] = [
        {
            "pr_number": 100,
            "title": "Refactor loading",
            "author": "a",
            "merged_at": datetime(2026, 2, 1),
            "merge_commit_sha": M1.encode(),
        },
        {
            "pr_number": 300,
            "title": "Touch the file only",
            "author": "c",
            "merged_at": datetime(2026, 2, 20),
            "merge_commit_sha": ("c" * 40).encode(),
        },
        {
            "pr_number": 400,
            "title": "Master-only PR the branch never took",
            "author": "d",
            "merged_at": datetime(2026, 2, 21),
            "merge_commit_sha": ("d" * 40).encode(),
        },
    ]
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=10,
            end_line=12,
            from_version="26.2",
            to_version="26.3",
        )
    )
    assert out.verdict == "candidates" and out.causes == [] and out.epistemic == "exact"
    assert [(c["pr_number"], c["lines_blamed"], c["evidence"]) for c in out.candidates] == [
        (100, 2, "both"),
        (200, 1, "blame"),
    ]  # #300 is outside the graph, #400 too
    assert out.latest_pr == 200 and out.lines["attributed"] == 3 and out.path_indexed
    assert out.window["to_sha"] == TO and out.window["base_sha"] == BASE
    assert out.window["sources"] == {"to": "namespace_head", "from": "mirror_ref"}
    assert out.legs["blame"]["status"] == "ok" and out.legs["blame"]["window_commits"] == 4
    assert out.legs["ledger"]["status"] == "ok" and not out.legs["blame"]["warm"]
    assert "LAST touched" in out.note and out.range == {
        "start": 10,
        "end": 12,
        "requested_end": 12,
        "truncated": False,
    }
    assert out.first_release_basis.startswith("branch-cut tags")
    # the ledger is read from this namespace only (it is the master entry)
    assert [c[0] for c in repo.window_calls] == [NS]
    since, until = repo.window_calls[0][2], repo.window_calls[0][3]
    assert since == datetime.fromtimestamp(1769000000, tz=UTC).replace(tzinfo=None)
    assert until == datetime.fromtimestamp(1771000000, tz=UTC).replace(tzinfo=None)
    blames = [c for c in git.calls if c[0] == "blame"]
    assert blames == [("blame", "-w", "-M", "--porcelain", "-L", "10,12", TO, "--", PATH)]

    # second call: blame and graph served from the cache — one subprocess fewer each
    before = len(git.calls)
    out2 = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=10,
            end_line=12,
            from_version="26.2",
            to_version="26.3",
        )
    )
    assert out2.legs["blame"]["warm"] is True
    assert not [c for c in git.calls[before:] if c[0] in ("blame", "rev-list")]


@pytest.mark.asyncio
async def test_release_namespace_reads_the_master_ledger_too_and_tracker_folds(wired) -> None:
    repo, git, state = wired
    repo.rows[NS] = [
        {
            "pr_number": 100,
            "title": "Refactor loading",
            "author": "a",
            "merged_at": datetime(2026, 2, 1),
            "merge_commit_sha": M1.encode(),
        }
    ]
    repo.rows["code-clickhouse-26-3"] = [
        {
            "pr_number": 200,
            "title": "Backport #199 to 26.3: Fix parts",
            "author": "b",
            "merged_at": datetime(2026, 3, 1),
            "merge_commit_sha": TO.encode(),
        }
    ]

    class _Source:
        async def prs_facts(self, org, ns, numbers, *, names=None):
            return {
                100: {"repo": "ClickHouse/ClickHouse", "has_test": 1, "minor_version": "26.3"},
                200: {"repo": "ClickHouse/ClickHouse", "has_test": -1},
            }

        async def edges_from(self, org, ns, refs, rels=None):
            return [
                {
                    "src_ref": "ClickHouse/ClickHouse#100",
                    "rel_type": "caused_by",
                    "dst_ref": "ClickHouse/ClickHouse#555",
                    "direction": "in",
                },
                {
                    "src_ref": "ClickHouse/ClickHouse#200",
                    "rel_type": "fixes",
                    "dst_ref": "ClickHouse/ClickHouse#444",
                    "direction": "out",
                },
            ]

    state["qa"], state["src"] = QA, _Source()
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace="code-clickhouse-26-3",
            path=PATH,
            start_line=10,
            end_line=12,
            from_sha=BASE,
        )
    )
    assert (
        out.verdict == "candidates" and out.tracker_namespace == QA and not out.tracker_unavailable
    )
    assert sorted(c[0] for c in repo.window_calls) == [NS, "code-clickhouse-26-3"]
    assert out.window["sources"] == {"to": "namespace_head", "from": "sha"}
    top, second = out.candidates
    assert top["pr_number"] == 100 and top["has_test"] is True and top["caused_by"] == [555]
    assert second["pr_number"] == 200 and second["fixes"] == [444] and "has_test" not in second
    assert second["first_release"] == "26.3" and second["evidence"] == "both"


@pytest.mark.asyncio
async def test_trunk_and_sibling_namespaces_are_read_only_when_the_caller_may_read_them(
    wired, monkeypatch
) -> None:
    """The transport gates the addressed namespace only: the trunk ledger and a sibling
    branch's head that LOOM adds on its own are left out when the caller cannot read them."""
    import server.auth.authz as authz

    repo, _git, _state = wired
    head_reads: list[str] = []
    all_state = repo.all_state

    async def _recorded_state(org: str, ns: str) -> dict[str, dict[str, Any]]:
        head_reads.append(ns)
        return await all_state(org, ns)

    repo.all_state = _recorded_state
    readable = {"code-clickhouse-26-3"}

    async def _only(org: str, ns: str, role: Any, *, hides_answer: bool = True) -> bool:
        return ns in readable

    monkeypatch.setattr(authz, "row_in_scope", _only)
    repo.rows[NS] = [
        {
            "pr_number": 100,
            "title": "Trunk-only title",
            "author": "a",
            "merged_at": datetime(2026, 2, 1),
            "merge_commit_sha": M1.encode(),
        }
    ]
    base = {"stage": "last_touched", "org": ORG, "path": PATH, "start_line": 10, "end_line": 12}
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(namespace="code-clickhouse-26-3", from_sha=BASE, **base)
    )
    assert [c[0] for c in repo.window_calls] == ["code-clickhouse-26-3"]
    # the shared mirror's blame may still name the PR; the trunk ledger's row does not reach it
    assert "Trunk-only title" not in str(out.candidates)
    assert all(c.get("evidence") == "blame" for c in out.candidates if c["pr_number"] == 100)

    readable = {NS}
    head_reads.clear()
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(namespace=NS, from_sha=BASE, to_version="26.3", **base)
    )
    assert "code-clickhouse-26-3" not in head_reads
    assert out.window["sources"]["to"] == "mirror_ref"


@pytest.mark.asyncio
async def test_blame_failure_falls_back_to_the_ledger_floor_and_names_the_cause(wired) -> None:
    repo, git, state = wired
    git.blame_error = "git blame rc=128: fatal: no such path 'x' in " + TO
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path="src/Nope.cpp",
            start_line=1,
            end_line=3,
            to_version="26.3",
        )
    )
    assert out.verdict == "unknown" and out.causes == ["path_not_indexed"]
    assert out.epistemic == "lower_bound" and not out.path_indexed and out.candidates == []
    assert out.window["from_ref"] == "v26.2.1.1-new" and out.window["sources"]["from"] == "default"

    git.blame_error = "git blame timed out after 5.0s"
    repo.rows[NS] = [
        {
            "pr_number": 300,
            "title": "Touch",
            "author": "c",
            "merged_at": datetime(2026, 2, 20),
            "merge_commit_sha": ("c" * 40).encode(),
        }
    ]
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=1,
            end_line=3,
            to_version="26.3",
        )
    )
    assert out.verdict == "candidates" and out.causes == ["blame_failed"]
    assert out.epistemic == "lower_bound" and out.candidates[0]["evidence"] == "ledger"
    assert out.legs["blame"]["status"] == "timeout" and out.latest_pr == 0

    # QA bound but down: the answer is complete, the cause is named, nothing blocks
    state["qa"] = QA

    class _Down:
        async def prs_facts(self, org, ns, numbers, *, names=None):
            raise TrackerUnavailable("breaker OPEN")

    state["src"] = _Down()
    git.blame_error = ""
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=10,
            end_line=12,
            to_version="26.3",
        )
    )
    assert (
        out.verdict == "candidates"
        and out.causes == ["tracker_unavailable"]
        and out.tracker_unavailable
    )


@pytest.mark.asyncio
async def test_unresolved_window_and_ledger_failure_are_named(wired) -> None:
    repo, git, _ = wired
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=1,
            end_line=3,
            to_version="27.9",
        )
    )
    assert out.verdict == "unknown" and out.causes == ["window_unresolved"]
    assert out.window["error"].startswith("to did not resolve") and out.candidates == []
    assert not [c for c in git.calls if c[0] == "blame"]

    repo.fail_ledger = True
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=10,
            end_line=12,
            to_version="26.3",
        )
    )
    assert out.verdict == "candidates" and out.causes == ["ledger_failed"]
    assert out.legs["ledger"]["status"] == "failed" and out.epistemic == "exact"
    assert [c["evidence"] for c in out.candidates] == ["blame", "blame"]


@pytest.mark.asyncio
async def test_wide_ranges_are_cut_at_400_lines(wired) -> None:
    _, git, _ = wired
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=1,
            end_line=5000,
            to_version="26.3",
        )
    )
    assert out.range == {"start": 1, "end": 400, "requested_end": 5000, "truncated": True}
    assert ("blame", "-w", "-M", "--porcelain", "-L", "1,400", TO, "--", PATH) in git.calls


@pytest.mark.asyncio
async def test_a_host_without_the_mirror_names_the_clone_and_runs_no_git(
    wired, monkeypatch, tmp_path
) -> None:
    """A configured repo whose mirror is not on this host yet answers no_git_mirror."""
    _, git, _ = wired
    asked: list[Any] = []

    async def _cloning(cfg: Any, **_kw: Any) -> bool:
        asked.append(cfg)
        return False

    from server.code.app.index import mirrors

    monkeypatch.setattr(mirrors, "mirror_ready", _cloning)
    (tmp_path / GIT).rmdir()
    out = await h.handle_code_blame_range(
        CodeBlameRangeInput(
            stage="last_touched",
            org=ORG,
            namespace=NS,
            path=PATH,
            start_line=10,
            end_line=12,
            to_version="26.3",
        )
    )
    assert {c.namespace for c in asked} == {NS}
    # Named by its label: a remote URL can carry credentials.
    assert out.window["error"] == f"no_git_mirror: cloning {asked[0].label}"
    assert "https://" not in out.window["error"]
    assert out.verdict == "unknown" and out.causes == ["window_unresolved"]
    assert out.candidates == [] and git.calls == []
