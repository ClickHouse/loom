"""code.first_build: a run that names the sha is exact, the
earliest descendant base is the carrier (lower_bound), the PR's own head
builds answer for an unmerged PR, `not_yet_built` only when every base in
the window was checked, and every missing input is a named cause."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

import server.code.app.first_build as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeFirstBuildInput
from server.code.app.index.gitrepo import GitError
from server.code.domain.index import first_build as fb
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
MERGE = "25a638e44500860043a7d8be3f77bee63b4a3399"
NEXT = "6621515ab836fece07ca628176010902d54fb0f9"  # master after MERGE
STALE = "775a2917aefd14defee8f263726ca54d86bdaf4b"  # master before MERGE
REL = "2c33b4b44cb9bab8a0a80b04d2301a76f8d5427c"  # a 26.3 head
HEAD = "abd4d87982a94af3ff515a5bc12ff92b904cb143"  # a PR head
T0 = datetime(2026, 9, 15, 11, 6, 59)


def _row(
    commit: str, base: str, pr: int, minutes: int, version: str = "26.9.1.1"
) -> dict[str, Any]:
    return {
        "check_start_time": T0 + timedelta(minutes=minutes),
        "pull_request_number": pr,
        "commit_sha": commit,
        "parent_commits_sha": [base, commit],
        "version": version,
        "docker_tag": "",
        "git_ref": "some-branch",
        "minor_version": version.rsplit(".", 2)[0],
    }


def _agg(
    base: str, minutes: int, builds: int, pr: int = 1, version: str = "26.9.1.1"
) -> dict[str, Any]:
    return {
        "base_sha": base,
        "first_seen": T0 + timedelta(minutes=minutes),
        "builds": builds,
        "first_pr": pr,
        "first_commit": "c" * 40,
        "first_version": version,
        "first_docker_tag": "",
        "first_git_ref": "b",
        "first_minor_version": version.rsplit(".", 2)[0],
    }


# ---- pure helpers -------------------------------------------------------------


def test_sha_normalizes_and_matches_by_prefix() -> None:
    assert fb.normalize_sha(" 25A638E ") == "25a638e" and fb.normalize_sha("xyz") == ""
    assert fb.normalize_sha("1" * 41) == "" and fb.normalize_sha("1" * 6) == ""
    assert fb.sha_matches(MERGE, "25a638e") and not fb.sha_matches(MERGE, NEXT[:7])


def test_split_named_separates_naming_rows_from_own_builds() -> None:
    rows = [
        _row(HEAD, STALE, 119986, -60),  # own build before the merge
        _row("d" * 40, MERGE, 120168, 40),  # a run whose base is the merge
        _row(MERGE, STALE, 118706, -10),  # the merge sha as a built commit (rare)
    ]
    named, own = fb.split_named(rows, MERGE, 119986)
    assert [b.kind for b in named] == ["named", "named"]
    assert [b.check_start_time for b in named] == [
        T0 - timedelta(minutes=10),
        T0 + timedelta(minutes=40),
    ]
    assert named[1].base_sha == MERGE and named[1].commit_sha == "d" * 40
    assert [b.commit_sha for b in own] == [HEAD] and own[0].kind == "own"


def test_candidates_sort_by_first_seen_and_later_builds_counts_the_window() -> None:
    cands = fb.candidates_of([_agg(NEXT, 30, 5), _agg(STALE, 5, 11), _agg(REL, 41, 1)])
    assert [c.base_sha for c in cands] == [STALE, NEXT, REL]
    carrier = cands[1]
    assert fb.later_builds(cands, carrier.first) == 4 + 1
    assert fb.lag_hours(T0, carrier.first_seen) == 0.5
    assert fb.lag_hours(None, T0) is None


def test_verdict_names_every_case() -> None:
    named = [fb.Build(kind="named", check_start_time=T0)]
    own = [fb.Build(kind="own", check_start_time=T0)]
    carrier = fb.BaseCandidate(base_sha=NEXT, first_seen=T0, builds=1)
    v = fb.verdict_of
    assert v(
        qa_ok=False,
        subject_known=True,
        named=named,
        own=[],
        carrier=None,
        checked=0,
        exhausted=False,
        merge_known=True,
    ) == ("unknown", ["tracker_unavailable"], "lower_bound")
    assert v(
        qa_ok=True,
        subject_known=True,
        named=named,
        own=[],
        carrier=None,
        checked=0,
        exhausted=False,
        merge_known=True,
    ) == ("built", [], "exact")
    assert v(
        qa_ok=True,
        subject_known=True,
        named=[],
        own=[],
        carrier=carrier,
        checked=3,
        exhausted=False,
        merge_known=True,
    ) == ("built", [], "lower_bound")
    assert v(
        qa_ok=True,
        subject_known=False,
        named=[],
        own=own,
        carrier=None,
        checked=0,
        exhausted=False,
        merge_known=False,
    ) == ("built", ["pr_not_merged"], "exact")
    assert v(
        qa_ok=True,
        subject_known=False,
        named=[],
        own=[],
        carrier=None,
        checked=0,
        exhausted=False,
        merge_known=False,
    ) == ("unknown", ["sha_unknown"], "lower_bound")
    assert v(
        qa_ok=True,
        subject_known=True,
        named=[],
        own=[],
        carrier=None,
        checked=10,
        exhausted=True,
        merge_known=True,
    ) == ("unknown", ["candidates_exhausted"], "lower_bound")
    assert v(
        qa_ok=True,
        subject_known=True,
        named=[],
        own=[],
        carrier=None,
        checked=4,
        exhausted=False,
        merge_known=True,
    ) == ("not_yet_built", [], "exact")


class _Cfg:
    def __init__(self, namespace: str, branch: str) -> None:
        self.org, self.namespace, self.git_branch = ORG, namespace, branch
        self.git_dir, self.repo = "/mirror.git", "ClickHouse/ClickHouse"
        self.git_remote = "https://github.com/ClickHouse/ClickHouse.git"


def test_nearest_namespace_prefers_the_lowest_release_branch() -> None:
    cfgs = [
        _Cfg(NS, "master"),
        _Cfg("code-clickhouse-26-6", "26.6"),
        _Cfg("code-clickhouse-26-8", "26.8"),
    ]
    assert fb.nearest_namespace(["master", "26.8", "26.6"], cfgs, NS) == (
        [NS, "code-clickhouse-26-6", "code-clickhouse-26-8"],
        "code-clickhouse-26-6",
    )
    assert fb.nearest_namespace(["master"], cfgs, NS) == ([NS], NS)
    assert fb.nearest_namespace([], cfgs, NS) == ([], "")
    assert fb.release_key("26.6") == (26, 6) and fb.release_key("master") is None


# ---- handler ------------------------------------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.ledger: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[int, str]] = []

    async def pr_merge_facts(self, org, ns, *, pr_number=0, merge_sha=""):
        self.calls.append((pr_number, merge_sha))
        if pr_number:
            return self.ledger.get(pr_number)
        for row in self.ledger.values():
            if row["merge_sha"].decode() == merge_sha:
                return row
        return None


class _Source:
    def __init__(self) -> None:
        self.naming: list[dict[str, Any]] = []
        self.after: list[dict[str, Any]] = []
        self.fail = False
        self.calls: list[tuple[str, Any]] = []

    async def builds_naming(self, org, ns, sha, pr_number=0):
        self.calls.append(("naming", (sha, pr_number)))
        if self.fail:
            raise TrackerUnavailable("breaker open")
        return list(self.naming)

    async def builds_after(self, org, ns, since, hours=72):
        self.calls.append(("after", (since, hours)))
        if self.fail:
            raise TrackerUnavailable("breaker open")
        return list(self.after)


class _Git:
    """The mirror: commits it holds (sha → (unix time, branches)) and the
    ancestry it answers."""

    def __init__(self) -> None:
        self.commits: dict[str, tuple[int, list[str]]] = {
            MERGE: (1789470419, ["master"]),
            NEXT: (1789475000, ["master"]),
            STALE: (1789460000, ["master"]),
            REL: (1789400000, ["26.3"]),
        }
        self.ancestors: set[tuple[str, str]] = {(MERGE, NEXT), (STALE, MERGE), (STALE, NEXT)}
        self.calls: list[tuple[str, ...]] = []

    async def run(self, git_dir: str, *args: str, timeout_s: float = 20.0) -> str:
        self.calls.append(args)
        assert args[0] == "log"
        sha = args[-1]
        full = next((s for s in self.commits if s.startswith(sha)), None)
        if full is None:
            raise GitError("fatal: bad revision")
        return f"{full} {self.commits[full][0]}\n"

    async def is_ancestor(self, git_dir: str, anc: str, desc: str, *, timeout_s: float = 20.0):
        self.calls.append(("is-ancestor", anc, desc))
        if desc not in self.commits:
            return None
        return anc == desc or (anc, desc) in self.ancestors

    async def branches_containing(self, git_dir: str, sha: str, *, timeout_s: float = 20.0):
        self.calls.append(("branch", sha))
        if sha not in self.commits:
            raise GitError("no such commit")
        return list(self.commits[sha][1])


@pytest.fixture
def wired(monkeypatch):
    from server.code.app.index import mirrors

    repo, git, src = _Repo(), _Git(), _Source()
    monkeypatch.setattr(mirrors, "_present", lambda cfg: True)
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(h.gitrepo, "run", git.run)
    monkeypatch.setattr(h.gitrepo, "is_ancestor", git.is_ancestor)
    monkeypatch.setattr(h.gitrepo, "branches_containing", git.branches_containing)
    import server.code.domain.index.repos as repos

    cfgs = [_Cfg(NS, "master"), _Cfg("code-clickhouse-26-3", "26.3")]
    monkeypatch.setattr(repos, "configured_repos", lambda: cfgs)
    state: dict[str, Any] = {"qa": QA}

    async def _authorized(org, code_ns, *, hides_answer=True):
        return state["qa"]

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: src)
    repo.ledger[118706] = {
        "pr_number": 118706,
        "merge_sha": MERGE.encode(),
        "merged_at": T0,
        "title": "t",
        "files": [],
    }
    return repo, git, src, state


def test_op_is_registered_experimental_viewer_read_and_input_is_checked() -> None:
    op = OPERATIONS_BY_NAME["code.first_build"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    with pytest.raises(ValueError):
        CodeFirstBuildInput(org=ORG, namespace=NS, sha="not-a-sha")
    assert CodeFirstBuildInput(org=ORG, namespace=NS, sha="25A638E").sha == "25A638E"
    # a keyless body parses: the namespace gate must answer a refused
    # principal before any shape complaint (the integration leak suite posts
    # pr_number=0 and no sha at every code read op)
    assert CodeFirstBuildInput(org=ORG, namespace=NS, pr_number=0).sha == ""


@pytest.mark.asyncio
async def test_keyless_or_short_sha_is_a_validation_error_after_the_gate(wired) -> None:
    from server.core.errors import ValidationError

    _, git, src, _ = wired
    with pytest.raises(ValidationError, match="sha or pr_number is required"):
        await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, pr_number=0))
    with pytest.raises(ValidationError, match="7–40 hex"):
        await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha="abc"))
    assert git.calls == [] and src.calls == []  # nothing was read for either


@pytest.mark.asyncio
async def test_a_run_whose_base_is_the_merge_is_exact(wired) -> None:
    repo, git, src, _ = wired
    src.naming = [_row(HEAD, STALE, 118706, -60), _row("d" * 40, MERGE, 120168, 40)]
    src.after = [_agg(STALE, 5, 11), _agg(MERGE, 40, 23), _agg(NEXT, 80, 5)]
    out = await h.handle_code_first_build(
        CodeFirstBuildInput(org=ORG, namespace=NS, pr_number=118706)
    )
    assert (out.verdict, out.epistemic, out.causes) == ("built", "exact", [])
    assert out.sha == MERGE and out.pr_number == 118706 and out.ledger_known and out.mirror_known
    assert out.first_build["kind"] == "named" and out.first_build["base_sha"] == MERGE
    assert (
        out.first_build["pull_request_number"] == 120168
        and out.first_build["version"] == "26.9.1.1"
    )
    assert out.lag_hours == pytest.approx(40 / 60, abs=0.01)
    assert out.later_builds == 22 + 5 and out.candidates_checked == 0
    assert [b["commit_sha"] for b in out.own_builds] == [HEAD]
    assert out.containing_branches == ["master"] and out.nearest_namespace == NS
    assert (
        out.tracker_namespace == QA and not out.tracker_unavailable and "PR runs only" in out.note
    )
    # the ledger was read by number, the tracker reads carried the merge sha and the merge time
    assert repo.calls == [(118706, "")]
    assert ("naming", (MERGE, 118706)) in src.calls and ("after", (T0, 72)) in src.calls
    assert not any(c[0] == "is-ancestor" for c in git.calls)


@pytest.mark.asyncio
async def test_the_earliest_descendant_base_is_the_carrier(wired) -> None:
    _, git, src, _ = wired
    src.after = [_agg(STALE, 5, 11), _agg(REL, 20, 1, version="26.3.33.87"), _agg(NEXT, 30, 5)]
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha=MERGE[:9]))
    assert (out.verdict, out.epistemic) == ("built", "lower_bound")
    assert out.sha == MERGE and out.pr_number == 118706  # the ledger named the PR by sha
    assert out.first_build["kind"] == "carrier" and out.first_build["base_sha"] == NEXT
    assert out.candidates_checked == 3 and out.candidates_total == 3 and out.later_builds == 4
    checks = [c for c in git.calls if c[0] == "is-ancestor"]
    assert checks == [
        ("is-ancestor", MERGE, STALE),
        ("is-ancestor", MERGE, REL),
        ("is-ancestor", MERGE, NEXT),
    ]


@pytest.mark.asyncio
async def test_all_bases_checked_and_stale_is_not_yet_built_but_a_cut_list_is_unknown(
    wired,
) -> None:
    _, git, src, _ = wired
    src.after = [_agg(STALE, 5, 11), _agg(REL, 20, 1)]
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha=MERGE))
    assert (out.verdict, out.causes, out.epistemic) == ("not_yet_built", [], "exact")
    assert out.first_build == {} and out.lag_hours is None and out.candidates_checked == 2
    # eleven stale bases: the tenth check stops and the answer is not a false negative
    src.after = [_agg(STALE, m, 1) for m in range(11)]
    for i, a in enumerate(src.after):
        a["base_sha"] = f"{i:040x}"
        git.commits[a["base_sha"]] = (1, ["master"])
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha=MERGE))
    assert (out.verdict, out.causes) == ("unknown", ["candidates_exhausted"])
    assert out.candidates_checked == 10 and out.candidates_total == 11


@pytest.mark.asyncio
async def test_an_unmerged_pr_answers_with_its_own_head_builds(wired) -> None:
    repo, git, src, _ = wired
    src.naming = [_row(HEAD, STALE, 119986, -60), _row("e" * 40, STALE, 119986, -30)]
    out = await h.handle_code_first_build(
        CodeFirstBuildInput(org=ORG, namespace=NS, pr_number=119986)
    )
    assert (out.verdict, out.causes, out.epistemic) == ("built", ["pr_not_merged"], "exact")
    assert out.sha == "" and not out.ledger_known and not out.mirror_known
    assert out.first_build["kind"] == "own" and out.first_build["commit_sha"] == HEAD
    assert len(out.own_builds) == 2 and out.containing_branches == []
    # no sha: no mirror read, no window read
    assert git.calls == [] and [c[0] for c in src.calls] == ["naming"]


@pytest.mark.asyncio
async def test_every_missing_input_is_a_named_cause(wired) -> None:
    _, git, src, state = wired
    # the tracker record cannot be read
    src.fail = True
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha=MERGE))
    assert (out.verdict, out.causes, out.tracker_unavailable) == (
        "unknown",
        ["tracker_unavailable"],
        True,
    )
    assert out.mirror_known and out.note == ""
    # nobody knows the sha
    src.fail = False
    src.calls.clear()
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha="f" * 40))
    assert (out.verdict, out.causes) == ("unknown", ["sha_unknown"])
    assert [c[0] for c in src.calls if c[0] == "after"] == []  # no merge time, no window
    # the code namespace has no QA binding
    state["qa"] = ""
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha=MERGE))
    assert (out.verdict, out.causes, out.tracker_namespace) == ("unknown", ["tracker_unbound"], "")
    assert out.containing_branches == ["master"]  # the mirror still spoke


@pytest.mark.asyncio
async def test_a_host_without_the_mirror_names_the_gap_in_causes(wired, monkeypatch) -> None:
    async def _cloning(cfg, op, *, head=""):
        return "", "no_git_mirror: cloning o/code-r"

    monkeypatch.setattr(h, "history_git_dir", _cloning)
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha=MERGE))
    assert not out.mirror_known
    assert out.causes[-1] == "no_git_mirror: cloning o/code-r"


@pytest.mark.asyncio
async def test_a_release_branch_sha_names_its_namespace(wired) -> None:
    _, git, src, _ = wired
    src.naming = [_row("e" * 40, REL, 120170, 41, version="26.3.33.87")]
    out = await h.handle_code_first_build(CodeFirstBuildInput(org=ORG, namespace=NS, sha=REL))
    assert out.verdict == "built" and out.first_build["minor_version"] == "26.3"
    assert out.containing_namespaces == ["code-clickhouse-26-3"]
    assert out.nearest_namespace == "code-clickhouse-26-3"


def test_sdk_and_mcp_surfaces_name_the_op() -> None:
    import inspect

    from sdk.python.loom.code import CodeClient as Code
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    params = inspect.signature(Code.first_build).parameters
    assert {"sha", "pr_number"} <= set(params)
    assert "not_yet_built" in TOOL_DESCRIPTIONS["code.first_build"]
    assert "master builds" in TOOL_DESCRIPTIONS["code.first_build"]
