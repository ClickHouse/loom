"""code.backport_gap: a merged fix names the release branches
that still carry the bug and the PR to cherry-pick there. The hunk headers
name the touched functions (truncated ones never), the record decides
`covered` / `pending` exactly, presence decides `gap` / `not_affected` as a
floor, an unavailable record with the code present is `unknown`, and the
branches come from the repository configuration, newest first."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.code.app.backport as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeBackportGapInput
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.tracker.domain import backport as bp
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
BRANCHES = {f"code-clickhouse-26-{i}": f"26.{i}" for i in range(1, 9)}
SRC = "src/DataTypes/Serializations/ISerialization.cpp"
HUNKS = [
    "std::unique_ptr<IDataType::SubcolumnInfo> IDataType::getSubcolumnInfo(",
    "void ISerialization::addElementToSubstreamsCache(ISerialization::SubstreamsCache",
    "ISerialization::ISubstreamsCacheElement * ISerialization::getElementFromSubstrea",
    "class ISerialization : private boost::noncopyable, public std::enable_shared_fro",
    "ReadBuffer * MergeTreeReaderWide::getStream(",
    "def test_substreams(self):",
    "def read_prefix(x):",
    "if (x)",
    "template <typename T> Foo<T>::run(",
]


# ---- pure helpers -------------------------------------------------------------


def test_hunk_symbols_keep_named_declarations_and_drop_truncated_ones() -> None:
    assert bp.hunk_symbols(HUNKS) == [
        "IDataType::getSubcolumnInfo",
        "ISerialization::addElementToSubstreamsCache",
        "ISerialization",
        "MergeTreeReaderWide::getStream",
        "read_prefix",
    ]
    # git cuts the context at 80 bytes: no `(` means the name may be cut too
    assert (
        bp.hunk_symbols(
            ["ISerialization::DeserializeBinaryBulkStatePtr ISerialization::getFromSubstreamsD"]
        )
        == []
    )
    assert bp.hunk_symbols(HUNKS, limit=2) == [
        "IDataType::getSubcolumnInfo",
        "ISerialization::addElementToSubstreamsCache",
    ]
    assert bp.parse_backport_title("Backport #115670 to 26.7: Fix x") == (115670, "26.7")
    assert bp.parse_backport_title("Fix x") == (0, "")
    assert bp.source_files(
        [SRC, "tests/queries/0_stateless/1.sql", "docs/a.md", SRC, "src/b.h"]
    ) == [SRC, "src/b.h"]


def test_branch_contains_reads_the_first_release() -> None:
    assert bp.branch_contains("26.7", "26.6") is True
    assert bp.branch_contains("26.6", "26.6") is True
    assert bp.branch_contains("26.5", "26.6") is False
    # unstamped fix: on master only, newer than every branch — no claim
    assert bp.branch_contains("26.8", "") is None
    assert bp.branch_contains("", "26.6") is None


def _inputs(**kw: Any) -> bp.BranchInputs:
    base: dict[str, Any] = dict(namespace="code-clickhouse-26-4", branch="26.4")
    base.update(kw)
    return bp.BranchInputs(**base)


def _verdict(inp: bp.BranchInputs, **kw: Any) -> bp.BranchVerdict:
    args: dict[str, Any] = dict(
        fix_minor="26.9",
        fix_versions=[],
        qa_ok=True,
        cherry_pick={"pr_number": 7, "merge_sha": "e" * 40},
    )
    args.update(kw)
    return bp.verdict_for(inp, **args)


def test_record_decides_covered_and_pending_exactly() -> None:
    merged = {"pr_number": 9, "state": "merged", "source": "qa"}
    v = _verdict(_inputs(backports=[merged], symbols={"f": "present"}))
    assert (v.verdict, v.epistemic, v.basis) == ("covered", "exact", "backport_pr")
    assert v.backport["pr_number"] == 9 and v.cherry_pick == {}
    # the ledger's row is the same answer with its own basis
    v = _verdict(_inputs(backports=[{**merged, "source": "ledger"}]))
    assert (v.verdict, v.basis) == ("covered", "ledger")
    # a branch cut at or after the fix's first release carries it natively
    v = _verdict(_inputs(branch="26.8", symbols={"f": "present"}), fix_minor="26.7")
    assert (v.verdict, v.epistemic, v.basis) == ("covered", "exact", "in_branch")
    # the issue's fix_versions names the branch but no PR: covered, a floor
    v = _verdict(_inputs(symbols={"f": "present"}), fix_versions=["26.4"])
    assert (v.verdict, v.epistemic, v.basis) == ("covered", "lower_bound", "fix_versions")
    v = _verdict(
        _inputs(
            backports=[{"pr_number": 8, "state": "open", "source": "qa"}], symbols={"f": "present"}
        )
    )
    assert (v.verdict, v.epistemic, v.backport["pr_number"]) == ("pending", "exact", 8)


def test_presence_decides_gap_and_not_affected_as_a_floor() -> None:
    v = _verdict(_inputs(symbols={"A::f": "present", "A::g": "absent"}))
    assert (v.verdict, v.epistemic, v.basis) == ("gap", "lower_bound", "symbol")
    assert v.cherry_pick == {"pr_number": 7, "merge_sha": "e" * 40}
    assert v.presence["symbols_present"] == ["A::f"] and v.presence["symbols_absent"] == ["A::g"]
    v = _verdict(_inputs(symbols={"A::f": "absent", "A::g": "absent"}))
    assert (v.verdict, v.basis, v.cherry_pick) == ("not_affected", "symbol", {})
    # an unknown is never folded into absent
    v = _verdict(
        _inputs(symbols={"A::f": "absent", "A::g": "unknown"}, symbol_reasons={"A::g": "deadline"})
    )
    assert (v.verdict, v.reason) == ("unknown", "deadline")
    # the record did not answer while the code is present: no gap claim
    v = _verdict(_inputs(symbols={"A::f": "present"}), qa_ok=False)
    assert (v.verdict, v.reason) == ("unknown", "tracker_unavailable")
    # no hunks: the file leg
    v = _verdict(_inputs(files_present=[SRC], files_checked=True))
    assert (v.verdict, v.basis, v.epistemic) == ("gap", "file", "lower_bound")
    assert v.evidence[-1].startswith("the fix carries no backport label")
    v = _verdict(_inputs(files_present=[SRC], files_checked=True), fix_reverted=True)
    assert (v.verdict, v.reason, v.cherry_pick) == ("unknown", "fix_reverted", {})
    v = _verdict(_inputs(files_absent=[SRC], files_checked=True, indexed=True))
    assert (v.verdict, v.basis) == ("not_affected", "file")
    v = _verdict(_inputs(files_absent=[SRC], files_checked=True, indexed=False))
    assert (v.verdict, v.reason) == ("unknown", "no_index")
    v = _verdict(_inputs(branch="", symbols={"A::f": "present"}))
    assert (v.verdict, v.reason) == ("unknown", "no_branch_version")
    v = _verdict(_inputs())
    assert (v.verdict, v.reason) == ("unknown", "no_touched_code")


def test_rank_summary_and_overall() -> None:
    vs = [
        bp.BranchVerdict("n8", "26.8", "covered", "exact", "in_branch"),
        bp.BranchVerdict("n3", "26.3", "gap", "lower_bound", "symbol"),
        bp.BranchVerdict("n5", "26.5", "gap", "lower_bound", "symbol"),
        bp.BranchVerdict("n6", "26.6", "pending", "exact", "backport_pr"),
        bp.BranchVerdict("n1", "26.1", "not_affected", "lower_bound", "symbol"),
    ]
    ranked = bp.rank(vs)
    assert [v.branch for v in ranked] == ["26.5", "26.3", "26.6", "26.1", "26.8"]
    assert bp.summarize(ranked)["gap"] == ["26.5", "26.3"]
    assert bp.overall(ranked, fix_found=True) == ("gap", [])
    assert bp.overall(ranked[2:], fix_found=True) == ("pending", [])
    assert bp.overall(ranked[3:], fix_found=True) == ("covered", [])
    assert bp.overall([ranked[3]], fix_found=True) == ("not_affected", [])
    assert bp.overall(ranked, fix_found=False) == ("unknown", ["no_fix"])
    assert bp.overall([], fix_found=True) == ("unknown", ["no_branches"])
    u = bp.BranchVerdict("n2", "26.2", "unknown", "lower_bound", "symbol", reason="deadline")
    assert bp.overall([vs[0], u], fix_found=True) == ("unknown", ["deadline"])


# ---- branches from the configuration -------------------------------------------


class _Cfg:
    def __init__(self, ns: str, branch: str, repo: str = "ClickHouse/ClickHouse") -> None:
        self.org, self.namespace, self.git_branch, self.repo = ORG, ns, branch, repo


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.code.domain.index.repos as repos

    cfgs = [
        _Cfg(NS, "master"),
        *(_Cfg(ns, v) for ns, v in BRANCHES.items()),
        _Cfg("code-oss-flask", "main", repo="pallets/flask"),
        _Cfg("code-other", "25.8", repo="x/y"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: cfgs)


def test_release_namespaces_are_the_same_repository_newest_first(configured) -> None:
    got = h.release_namespaces(ORG, NS)
    assert got[0] == ("code-clickhouse-26-8", "26.8") and got[-1] == (
        "code-clickhouse-26-1",
        "26.1",
    )
    assert len(got) == 8 and all(ns.startswith("code-clickhouse-26-") for ns, _ in got)
    # a version, a namespace, and a branch loom does not index
    assert h.resolve_branches(ORG, NS, ["26.4", "code-clickhouse-26-6", "25.8", "26.4"]) == [
        ("code-clickhouse-26-4", "26.4"),
        ("code-clickhouse-26-6", "26.6"),
        ("25.8", "25.8"),
    ]


# ---- the handler -------------------------------------------------------------------


class _Repo:
    def __init__(
        self,
        *,
        present: dict[str, set[str]] | None = None,
        ledger_titled: dict[str, list] | None = None,
        files: dict[str, set[str]] | None = None,
        fix: dict[str, Any] | None = None,
    ) -> None:
        self.present, self.ledger_titled, self.files = (
            present or {},
            ledger_titled or {},
            files or {},
        )
        self.fix = fix
        self.calls: list[tuple[str, str]] = []

    async def pr_facts(self, org, ns, pr_number):
        self.calls.append(("fix", ns))
        return self.fix

    async def backport_prs_titled(self, org, ns, upstream, *, limit=8):
        self.calls.append(("ledger", ns))
        return list(self.ledger_titled.get(ns, []))

    async def symbol_names_present(self, org, ns, names):
        self.calls.append(("names", ns))
        return {n: [n] for n in names if n in self.present.get(ns, set())}

    async def live_paths_present(self, org, ns, paths):
        self.calls.append(("files", ns))
        return set(self.files.get(ns, set()))


class _Source:
    def __init__(
        self,
        *,
        change: dict[str, Any] | None = None,
        backports: list | None = None,
        issue: dict[str, Any] | None = None,
        fixes: list | None = None,
        down: bool = False,
    ) -> None:
        self.change, self.backports, self.issue, self.fixes, self.down = (
            change,
            backports or [],
            issue,
            fixes or [],
            down,
        )

    def repos(self):
        return ["ClickHouse/ClickHouse"]

    async def items_by_number(self, org, ns, nums):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        return {n: self.issue for n in nums if self.issue and n == self.issue.get("number")}

    async def fix_prs_of_issue(self, org, ns, number):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        return list(self.fixes)

    async def pr_change(self, org, ns, number):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        return dict(self.change) if self.change and self.change["pr_number"] == number else None

    async def backport_prs_of(self, org, ns, number):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        return list(self.backports)


CHANGE = {
    "pr_number": 115670,
    "title": "Fix substreams cache key collision",
    "url": "u",
    "author": "a",
    "base_branch": "master",
    "minor_version": "26.9",
    "is_merged": 1,
    "is_backport_pr": 0,
    "is_cherrypick_pr": 0,
    "is_reverted": 0,
    "upstream_pr_number": 0,
    "changelog_category": "critical_bug_fix",
    "src_components": ["DataTypes"],
    "files": [SRC, "tests/queries/0_stateless/04927_x.sql"],
    "merged_at": datetime(2026, 9, 15, 23, 11),
    "hunk_contexts": HUNKS,
}
LEDGER_FIX = {
    "pr_number": 115670,
    "title": "Fix substreams cache key collision",
    "files": [SRC],
    "labels": ["pr-must-backport", "pr-critical-bugfix"],
    "merge_commit_sha": b"e6c985c411a476adc5279d62b8b2dc56f0ddd592",
    "merged_at": datetime(2026, 9, 15, 23, 11),
}
SYMS = [
    "IDataType::getSubcolumnInfo",
    "ISerialization::addElementToSubstreamsCache",
    "ISerialization",
    "MergeTreeReaderWide::getStream",
    "read_prefix",
]


def _bp(n: int, branch: str, state: str) -> dict[str, Any]:
    return {
        "pr_number": n,
        "title": f"Backport #115670 to {branch}: Fix",
        "url": f"u/{n}",
        "base_branch": branch,
        "minor_version": branch,
        "state": state,
        "merged_at": datetime(2026, 9, 16) if state == "merged" else None,
        "source": "qa",
    }


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, configured) -> Any:
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    async def _authorized(org, code_ns, *, hides_answer=True):
        return QA if code_ns == NS else ""

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)

    async def _as_of(repo, org, ns):
        return {"commit": f"{ns}-head", "status": "fresh"} if ns in BRANCHES else {}

    monkeypatch.setattr(h, "index_as_of", _as_of)

    async def _authorize(org, namespaces, *, op="code.search"):
        return None

    import server.code.app.search_fanout as fanout

    monkeypatch.setattr(fanout, "authorize", _authorize)
    holder: dict[str, Any] = {}

    def _use(source: _Source, repo: _Repo) -> None:
        holder["src"], holder["repo"] = source, repo

    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])
    monkeypatch.setattr(h, "CodeRepo", lambda: holder["repo"])
    return _use


def test_op_is_registered_experimental_viewer_read() -> None:
    op = OPERATIONS_BY_NAME["code.backport_gap"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    inp = CodeBackportGapInput(
        org=ORG, namespace=NS, pr_number=1, branches=[" 26.4 ", "26.4", "26.5"]
    )
    assert inp.branches == ["26.4", "26.5"] and inp.max_symbols == 8


@pytest.mark.asyncio
async def test_pr_path_composes_record_release_and_presence(wired) -> None:
    present = {ns: {SYMS[1], SYMS[2], SYMS[3]} for ns in BRANCHES}
    present["code-clickhouse-26-1"] = set()  # the code is not on 26.1
    repo = _Repo(
        present=present,
        fix=LEDGER_FIX,
        ledger_titled={
            "code-clickhouse-26-5": [
                {
                    "pr_number": 120400,
                    "pr_title": "Backport #115670 to 26.5: Fix",
                    "merge_sha": b"a" * 40,
                    "merged": datetime(2026, 9, 16),
                }
            ]
        },
    )
    wired(
        _Source(
            change=CHANGE, backports=[_bp(120302, "26.8", "merged"), _bp(120300, "26.6", "open")]
        ),
        repo,
    )
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, consumer="t")
    )
    assert (
        out.verdict == "gap"
        and out.causes == []
        and out.tracker_namespace == QA
        and not out.tracker_unavailable
    )
    assert out.symbols == SYMS and out.files == [SRC]
    assert (
        out.fix["merge_sha"] == "e6c985c411a476adc5279d62b8b2dc56f0ddd592"
        and out.fix["ledger"] is True
    )
    assert out.fix["minor_version"] == "26.9" and out.fix["merged_at"].startswith("2026-09-15")
    assert out.fix["backport_required"] is True and out.fix["labels"] == [
        "pr-must-backport",
        "pr-critical-bugfix",
    ]
    by = {b["branch"]: b for b in out.branches}
    assert [b["branch"] for b in out.branches] == [
        "26.7",
        "26.4",
        "26.3",
        "26.2",
        "26.6",
        "26.1",
        "26.8",
        "26.5",
    ]
    assert (
        by["26.8"]["verdict"] == "covered"
        and by["26.8"]["basis"] == "backport_pr"
        and by["26.8"]["epistemic"] == "exact"
    )
    assert by["26.8"]["backport"]["pr_number"] == 120302 and by["26.8"]["backport"][
        "merged_at"
    ].startswith("2026-09-16")
    assert (
        by["26.5"]["verdict"] == "covered"
        and by["26.5"]["basis"] == "ledger"
        and by["26.5"]["backport"]["merge_sha"] == "a" * 40
    )
    assert by["26.6"]["verdict"] == "pending" and by["26.6"]["backport"]["state"] == "open"
    gap = by["26.7"]
    assert (
        gap["verdict"] == "gap" and gap["epistemic"] == "lower_bound" and gap["basis"] == "symbol"
    )
    assert gap["cherry_pick"] == {
        "pr_number": 115670,
        "merge_sha": "e6c985c411a476adc5279d62b8b2dc56f0ddd592",
    }
    assert gap["presence"]["symbols_present"] == sorted([SYMS[1], SYMS[2], SYMS[3]])
    assert gap["evidence"][-1].startswith("the fix is labelled for backport")
    assert gap["presence"]["symbols_absent"] == sorted([SYMS[0], SYMS[4]])
    assert by["26.1"]["verdict"] == "not_affected" and by["26.1"]["cherry_pick"] == {}
    assert out.summary["gap"] == ["26.7", "26.4", "26.3", "26.2"] and out.summary["covered"] == [
        "26.8",
        "26.5",
    ]
    # one name-keyed read per branch, never one per symbol; the ledger leg on every branch
    assert sum(1 for k, _ in repo.calls if k == "names") == 8
    assert (
        sum(1 for k, _ in repo.calls if k == "ledger") == 8
        and ("files", "code-clickhouse-26-4") not in repo.calls
    )
    assert set(out.stage_ms) == {"fix", "presence"}


@pytest.mark.asyncio
async def test_branch_cut_after_the_fix_is_covered_and_unstamped_fix_claims_nothing(wired) -> None:
    repo = _Repo(present={ns: {SYMS[1]} for ns in BRANCHES}, fix=LEDGER_FIX)
    wired(_Source(change={**CHANGE, "minor_version": "26.6"}), repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(
            org=ORG, namespace=NS, pr_number=115670, branches=["26.8", "26.6", "26.5"]
        )
    )
    by = {b["branch"]: b for b in out.branches}
    assert by["26.8"]["basis"] == "in_branch" and by["26.6"]["basis"] == "in_branch"
    assert by["26.5"]["verdict"] == "gap" and out.summary["gap"] == ["26.5"]
    # no first release yet: master only, every branch is tested on presence
    wired(_Source(change={**CHANGE, "minor_version": ""}), repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, branches=["26.8"])
    )
    assert out.branches[0]["verdict"] == "gap"


@pytest.mark.asyncio
async def test_issue_path_picks_the_trunk_fix_and_reads_fix_versions(wired) -> None:
    issue = {
        "repo": "ClickHouse/ClickHouse",
        "number": 118736,
        "is_pr": 0,
        "title": "regression",
        "state": "closed",
        "affected_versions": [],
        "fix_versions": ["26.8", "26.9"],
    }
    fixes = [
        {
            "pr_number": 120293,
            "title": "Backport #119517 to 26.8: Cache",
            "is_merged": 1,
            "is_backport_pr": 1,
            "is_cherrypick_pr": 0,
            "base_branch": "26.8",
            "minor_version": "26.8",
        },
        {
            "pr_number": 119517,
            "title": "Cache the hash",
            "is_merged": 1,
            "is_backport_pr": 0,
            "is_cherrypick_pr": 0,
            "base_branch": "master",
            "minor_version": "26.9",
        },
        {
            "pr_number": 120291,
            "title": "Cherry pick #119517 to 26.6: Cache",
            "is_merged": 0,
            "is_backport_pr": 0,
            "is_cherrypick_pr": 0,
            "base_branch": "backport/26.6/119517",
            "minor_version": "",
        },
    ]
    change = {
        **CHANGE,
        "pr_number": 119517,
        "hunk_contexts": [],
        "files": ["src/Analyzer/TableNode.cpp"],
    }
    repo = _Repo(
        files={
            ns: {"src/Analyzer/TableNode.cpp"} for ns in BRANCHES if ns != "code-clickhouse-26-1"
        },
        fix={**LEDGER_FIX, "pr_number": 119517},
    )
    wired(_Source(change=change, issue=issue, fixes=fixes), repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(
            org=ORG, namespace=NS, issue_number=118736, branches=["26.8", "26.7", "26.1"]
        )
    )
    assert out.fix["pr_number"] == 119517 and out.issue["fixed_by"] == [120293, 119517, 120291]
    assert out.issue["fix_versions"] == ["26.8", "26.9"] and len(out.fixes) == 3
    by = {b["branch"]: b for b in out.branches}
    # no hunks → the file leg; fix_versions covers 26.8 without a named PR
    assert out.symbols == [] and out.files == ["src/Analyzer/TableNode.cpp"]
    assert (
        by["26.8"]["verdict"] == "covered"
        and by["26.8"]["basis"] == "fix_versions"
        and by["26.8"]["epistemic"] == "lower_bound"
    )
    assert by["26.7"]["verdict"] == "gap" and by["26.7"]["basis"] == "file"
    assert by["26.1"]["verdict"] == "not_affected" and by["26.1"]["basis"] == "file"
    assert ("names", "code-clickhouse-26-8") not in repo.calls


@pytest.mark.asyncio
async def test_unavailable_record_and_unknown_fix_are_named(wired) -> None:
    repo = _Repo(files={ns: {SRC} for ns in BRANCHES}, fix=LEDGER_FIX)
    wired(_Source(down=True), repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, branches=["26.4"])
    )
    # the ledger still names the fix; no hunks from QA → file leg; code present, record silent
    assert out.tracker_unavailable and out.causes == ["tracker_unavailable"]
    assert out.fix["merge_sha"].startswith("e6c985") and out.symbols == [] and out.files == [SRC]
    assert (
        out.branches[0]["verdict"] == "unknown"
        and out.branches[0]["reason"] == "tracker_unavailable"
    )
    # neither record nor ledger knows the number
    wired(_Source(change=None), _Repo(fix=None))
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=1, branches=["26.4"])
    )
    assert out.verdict == "unknown" and out.causes == ["fix_not_found", "no_fix"]
    assert out.branches[0]["reason"] == "no_fix"
    assert out.fix == {
        "pr_number": 1,
        "merge_sha": "",
        "ledger": False,
        "labels": [],
        "backport_required": False,
    }
    # a reverted fix is never a pick
    wired(
        _Source(change={**CHANGE, "is_reverted": 1}),
        _Repo(present={ns: {SYMS[1]} for ns in BRANCHES}, fix=LEDGER_FIX),
    )
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, branches=["26.4"])
    )
    assert out.verdict == "unknown" and out.causes == ["fix_reverted"]
    assert out.branches[0]["reason"] == "fix_reverted" and out.branches[0]["cherry_pick"] == {}


@pytest.mark.asyncio
async def test_unbound_code_namespace_reads_the_ledger_only(wired, monkeypatch) -> None:
    async def _none(org, code_ns, *, hides_answer=True):
        return ""

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _none)
    repo = _Repo(
        files={ns: {SRC} for ns in BRANCHES},
        fix=LEDGER_FIX,
        ledger_titled={
            "code-clickhouse-26-4": [
                {
                    "pr_number": 5,
                    "pr_title": "Backport #115670 to 26.4: Fix",
                    "merge_sha": b"b" * 40,
                    "merged": datetime(2026, 9, 1),
                }
            ]
        },
    )
    wired(_Source(), repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, branches=["26.4", "26.3"])
    )
    assert (
        out.causes == ["tracker_unbound"]
        and out.tracker_namespace == ""
        and not out.tracker_unavailable
    )
    by = {b["branch"]: b for b in out.branches}
    assert by["26.4"]["verdict"] == "covered" and by["26.4"]["basis"] == "ledger"
    assert by["26.3"]["verdict"] == "unknown" and by["26.3"]["reason"] == "tracker_unbound"


@pytest.mark.asyncio
async def test_presence_deadline_is_unknown_never_absent(wired, monkeypatch) -> None:
    import asyncio

    class _Slow(_Repo):
        async def symbol_names_present(self, org, ns, names):
            if ns == "code-clickhouse-26-3":
                await asyncio.sleep(0.3)
            return await super().symbol_names_present(org, ns, names)

    monkeypatch.setattr(h, "_PRESENCE_WALL_MS", 50)
    repo = _Slow(present={ns: {SYMS[1]} for ns in BRANCHES}, fix=LEDGER_FIX)
    wired(_Source(change=CHANGE), repo)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, branches=["26.4", "26.3"])
    )
    by = {b["branch"]: b for b in out.branches}
    assert by["26.4"]["verdict"] == "gap"
    assert by["26.3"]["verdict"] == "unknown" and by["26.3"]["reason"] == "deadline"
    assert by["26.3"]["presence"]["symbols_unknown"] == sorted(SYMS) and out.verdict == "gap"


@pytest.mark.asyncio
async def test_sdk_sends_only_the_named_fields() -> None:
    from sdk.python.loom.code import CodeClient

    class _Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((path, body))
            return {}

    class _Agent:
        def __init__(self) -> None:
            self.transport = _Transport()

    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.backport_gap(115670, branches=["26.4"])
    assert ag.transport.calls[0] == (
        "/v1/code.backport_gap",
        {"max_symbols": 8, "pr_number": 115670, "branches": ["26.4"]},
    )
    await c.backport_gap(issue_number=118736)
    assert ag.transport.calls[1][1] == {"max_symbols": 8, "issue_number": 118736}


@pytest.mark.asyncio
async def test_missing_subject_is_refused_in_the_handler_after_the_gate(wired) -> None:
    """The "pr_number or issue_number" rule lives in the handler, after the
    namespace gate (as the review brief does), so a refused principal never learns the
    op's argument shape from a model-level 422."""
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError):
        await h.handle_code_backport_gap(CodeBackportGapInput(org=ORG, namespace=NS))
