"""The set-wide backport report and the history leg: the
probed lines and their regex, the per-branch verdict from the mirror's
answers, `refine` touching only the floored rows the lines left undecided,
the real git plumbing on a throwaway repository, the handler running the
history stage for the undecided branches only and serving the exact-gap payload
byte for byte without the flag, the nightly worker (ordering, the settled
skip, the cap and budget, batching, the QA-miss deferral, the table rows and
the outcome scalars), the `code.backport_gaps` read (ranking, counts, the
namespace gate, staleness), the repository rows, and the SDK bodies."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import server.code.app.backport as h
import server.code.app.backport_gaps as hg
import server.code.workers.backport_gaps as wk
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeBackportGapInput, CodeBackportGapsInput
from server.code.store import backport_gaps as gr
from server.core.errors import AuthInsufficientError
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.app import patch_history as ph
from server.tracker.domain import backport as bp
from server.tracker.domain import patch_presence as pp

pytestmark = pytest.mark.usefixtures("mirror_here")

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
BRANCHES = {f"code-clickhouse-26-{i}": f"26.{i}" for i in range(1, 9)}
SRC = "src/DataTypes/IDataType.cpp"
NOW = datetime(2026, 9, 16, 12, 0, 0)


@dataclass
class _Cfg:
    namespace: str
    git_branch: str
    org: str = ORG
    repo: str = "ClickHouse/ClickHouse"
    git_dir: str = "var/code_repos/clickhouse.git"
    classification: str = "standard"
    source: str = "config"


def _groups() -> list[pp.HunkGroup]:
    g = pp.HunkGroup(path=SRC, symbol="IDataType::getSubcolumnInfo", hunks=1)
    g.added = [pp.normalise("auto cache_key = SubstreamsCacheKey::fromPath(subpath, prefix_len);")]
    g.removed = [
        pp.normalise(
            "auto name = ISerialization::getSubcolumnNameForStream(subpath, prefix_len, false);"
        ),
        pp.normalise("if (name == requested_name)"),
        pp.normalise("return makeSubcolumnInfo(subpath, prefix_len);"),
        pp.normalise("moved_line(x, y);"),
        pp.normalise("}"),
    ]
    g.added.append(pp.normalise("moved_line(x, y);"))  # a move counts on neither side
    return [g]


# ---------------------------------------------------------------- pure half


def test_path_lines_take_the_informative_lines_and_never_a_move() -> None:
    per = ph.path_lines(_groups())
    assert list(per) == [SRC]
    added, removed = per[SRC]
    assert added == [
        pp.normalise("auto cache_key = SubstreamsCacheKey::fromPath(subpath, prefix_len);")
    ]
    assert (
        removed[0].startswith("auto name = ISerialization") and len(removed) == 3
    )  # longest first
    assert all("moved_line" not in ln and ln != "}" for ln in removed)
    assert ph.path_lines([pp.HunkGroup(path="", symbol="")]) == {}


def test_line_regex_escapes_ere_and_widens_whitespace() -> None:
    rx = ph.line_regex(pp.normalise("if (a[i] == b.c* + 1) { return $x; }"))
    assert rx == (
        r"if[[:space:]]+\(a\[i\][[:space:]]+==[[:space:]]+b\.c\*[[:space:]]+\+[[:space:]]+1\)"
        r"[[:space:]]+\{[[:space:]]+return[[:space:]]+\$x;[[:space:]]+\}"
    )
    assert ph.line_regex("plain_token") == "plain_token"


def _tip(
    added: tuple[int, int], removed: tuple[int, int], *, paths: int = 1, paths_absent: int = 0
) -> ph.HistoryPresence:
    hp = ph.HistoryPresence(
        added_total=added[1],
        added_present=added[0],
        removed_total=removed[1],
        removed_present=removed[0],
        paths=paths,
        paths_absent=paths_absent,
    )
    ph.decide_tip(hp)
    return hp


def test_decide_tip_applies_the_exact_rules_to_what_the_tip_holds() -> None:
    fixed = _tip((5, 5), (0, 3))
    assert (fixed.verdict, fixed.epistemic, fixed.reason, fixed.mode) == (
        "not_affected",
        "exact",
        "fixed_at_tip",
        ph.EXACT,
    )
    bug = _tip((0, 5), (3, 3))
    assert (bug.verdict, bug.epistemic) == ("gap", "exact")
    assert _tip((1, 5), (2, 3)).verdict == "gap"  # the pre-image intact, whatever matched by chance
    assert _tip((0, 3), (0, 0)).verdict == "gap"  # a pure addition absent (the exact-gap rule)
    assert (
        _tip((3, 3), (0, 1)).verdict == "" and _tip((3, 3), (0, 1)).reason == "undecided"
    )  # one removed line
    assert _tip((2, 5), (0, 3)).reason == "undecided"  # neither image: the history's turn
    assert _tip((1, 1), (0, 0)).reason == "too_few_lines"


def _hist(*states: str, commits: tuple[str, ...] = ()) -> ph.HistoryPresence:
    hp = ph.HistoryPresence(
        added_total=4, added_present=1, removed_total=3, removed_present=0, paths=1
    )
    for i, st in enumerate(states):
        hp.lines.append(
            ph.LineHistory(SRC, f"line {i}", st, commit=commits[i] if i < len(commits) else "")
        )
    ph.decide_history(hp)
    return hp


def test_decide_history_names_restructured_or_removed_or_stays_undecided() -> None:
    never = _hist("never", "never")
    assert (never.verdict, never.epistemic, never.reason) == (
        "not_affected",
        "lower_bound",
        "restructured",
    )
    removed = _hist("never", "removed", commits=("", "abc123"))
    assert (removed.verdict, removed.reason) == ("not_affected", "removed_on_branch")
    assert (
        _hist("never", "deadline").verdict == "" and _hist("never", "deadline").reason == "deadline"
    )
    assert _hist("never", "error:git").reason == "error:git"
    assert _hist().reason == "no_probes"
    d = never.as_dict()
    assert (
        d["verdict"] == "not_affected"
        and d["probes"] == 2
        and d["never"] == 2
        and d["removed"] == 0
    )
    assert d["added_total"] == 4 and d["removed_present"] == 0 and d["lines"][0]["state"] == "never"


def test_names_fix_reads_the_pr_number_or_the_merge_sha() -> None:
    assert ph.names_fix("Backport #115670 to 26.7: Fix", "", pr_number=115670, merge_sha="")
    assert ph.names_fix(
        "Merge pull request #1 from x",
        "(cherry picked from commit e1e2e3e4e5e6)",
        pr_number=2,
        merge_sha="E1E2E3E4E5E6F7",
    )
    assert not ph.names_fix(
        "Fix #1156701", "", pr_number=115670, merge_sha=""
    )  # a longer number is another PR
    assert not ph.names_fix("Fix", "", pr_number=0, merge_sha="")


def test_history_leg_names_the_read_the_tip_calls_for() -> None:
    def leg(a, r):
        return ph.history_leg(
            ph.HistoryPresence(
                added_total=a[1], added_present=a[0], removed_total=r[1], removed_present=r[0]
            )
        )

    assert (
        leg((0, 5), (0, 3)) == "removed"
        and leg((1, 5), (1, 3)) == "removed"
        and leg((0, 0), (1, 3)) == "removed"
    )
    assert (
        leg((0, 5), (2, 3)) == "" and leg((2, 5), (0, 3)) == ""
    )  # a remnant over half, or the fix half there
    assert leg((0, 5), (3, 3)) == ""  # the pre-image intact is the tip's call (gap), not history's
    assert (
        leg((4, 5), (0, 0)) == "added"
        and leg((5, 5), (1, 3)) == "added"
        and leg((1, 1), (0, 0)) == ""
    )


def test_decide_origin_needs_a_commit_that_names_the_fix() -> None:
    hp = ph.HistoryPresence(added_total=3, added_present=3)
    hp.lines = [
        ph.LineHistory(SRC, "a", "added", commit="abc"),
        ph.LineHistory(SRC, "b", "picked", commit="def", subject="Backport #1"),
    ]
    ph.decide_origin(hp)
    assert (hp.verdict, hp.epistemic, hp.reason) == ("not_affected", "exact", "picked")
    hp = ph.HistoryPresence(added_total=3, added_present=3)
    hp.lines = [ph.LineHistory(SRC, "a", "added", commit="abc")]
    ph.decide_origin(hp)
    assert hp.verdict == "" and hp.reason == "unnamed_origin"
    hp.lines.append(ph.LineHistory(SRC, "b", "deadline"))
    ph.decide_origin(hp)
    assert hp.reason == "deadline"
    d = hp.as_dict()
    assert d["lines"][0] == {"path": SRC, "line": "a", "state": "added", "commit": "abc"}


def _v(
    verdict: str, epistemic: str, basis: str, patch_reason: str | None = "undecided"
) -> bp.BranchVerdict:
    v = bp.BranchVerdict(
        namespace="code-clickhouse-26-6",
        branch="26.6",
        verdict=verdict,
        epistemic=epistemic,
        basis=basis,
    )
    if patch_reason is not None:
        v.presence["patch"] = {"mode": "symbol_only", "reason": patch_reason}
    return v


def test_refine_moves_only_floored_undecided_rows() -> None:
    pick = {"pr_number": 115670, "merge_sha": "e" * 40}
    v = _v("gap", "lower_bound", "symbol")
    ph.refine(v, _tip((0, 5), (3, 3)), pick)
    assert (v.verdict, v.epistemic, v.basis, v.reason) == ("gap", "exact", "history", "")
    assert v.cherry_pick == pick and "pre-image is on the branch" in v.evidence[-1]
    assert v.presence["history"]["verdict"] == "gap"
    v = _v("gap", "lower_bound", "symbol")
    ph.refine(v, _tip((5, 5), (0, 3)), pick)
    assert (v.verdict, v.epistemic, v.basis, v.reason) == ("not_affected", "exact", "history", "")
    assert v.cherry_pick == {} and "holds the fixed state" in v.evidence[-1]
    v = _v("gap", "lower_bound", "symbol")
    ph.refine(v, _hist("never", "never"), pick)
    assert (v.verdict, v.epistemic, v.basis, v.reason) == (
        "not_affected",
        "lower_bound",
        "history",
        "restructured",
    )
    assert "another shape" in v.evidence[-1]
    v = _v("not_affected", "lower_bound", "symbol")
    ph.refine(v, _hist("never", "removed", commits=("", "abc123def456")), pick)
    assert (v.verdict, v.basis, v.reason) == ("not_affected", "history", "removed_on_branch")
    assert "abc123def456" in v.evidence[-1]
    picked = ph.HistoryPresence(
        mode=ph.EXACT,
        verdict="not_affected",
        epistemic="exact",
        reason=ph.PICKED,
        lines=[
            ph.LineHistory(
                SRC, "a", "picked", commit="abc123def456", subject="Backport #115670 to 26.6"
            )
        ],
    )
    v = _v("gap", "lower_bound", "symbol")
    ph.refine(v, picked, pick)
    assert (v.verdict, v.epistemic, v.basis, v.reason) == ("not_affected", "exact", "history", "")
    assert "abc123def456 Backport #115670" in v.evidence[-1]
    partial = ph.HistoryPresence(
        mode=ph.EXACT,
        verdict="not_affected",
        epistemic="lower_bound",
        reason="restructured_partial",
        removed_total=3,
        removed_present=1,
        lines=[ph.LineHistory(SRC, "a", "never"), ph.LineHistory(SRC, "b", "never")],
    )
    v = _v("gap", "lower_bound", "symbol")
    ph.refine(v, partial, pick)
    assert v.reason == "restructured_partial" and "1/3 of the fix's removed lines" in v.evidence[-1]
    absent = ph.HistoryPresence(
        mode=ph.EXACT,
        verdict="not_affected",
        epistemic="lower_bound",
        reason=ph.PATH_ABSENT,
        paths=2,
        paths_absent=2,
    )
    v = _v("gap", "lower_bound", "file")
    ph.refine(v, absent, pick)
    assert (v.verdict, v.reason) == (
        "not_affected",
        "path_absent",
    ) and "does not have the code" in v.evidence[-1]
    # the lines decided → untouched; the record decided → untouched;
    # an undecided history → untouched but disclosed
    for v, hp in (
        (_v("gap", "exact", "hunk", patch_reason=None), _hist("never", "never")),
        (_v("covered", "exact", "backport_pr"), _tip((0, 5), (3, 3))),
        (_v("unknown", "lower_bound", "symbol"), _tip((0, 5), (3, 3))),
        (_v("gap", "lower_bound", "symbol", patch_reason="no_chunks"), _tip((0, 5), (3, 3))),
        (_v("gap", "lower_bound", "symbol"), _tip((2, 5), (0, 3))),
    ):
        before = (v.verdict, v.epistemic, v.basis)
        ph.refine(v, hp, pick)
        assert (v.verdict, v.epistemic, v.basis) == before and "history" in v.presence


# ---------------------------------------------------------------- the mirror


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    base = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00",
        "HOME": str(cwd),
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
    ).stdout


@pytest.fixture
def mirror(tmp_path: Path) -> tuple[str, str]:
    """A bare mirror with branch `rel`: one file whose history added a line,
    then removed it; another line that was never there."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "rel")
    src = work / "a.cpp"
    src.write_text("int keep_me(int x)\n{\n    return x;\n}\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "one")
    src.write_text("int keep_me(int x)\n{\n    auto  tmp =   compute(x, 1);\n    return x;\n}\n")
    _git(work, "commit", "-q", "-am", "two")
    src.write_text("int keep_me(int x)\n{\n    return x;\n}\n")
    _git(work, "commit", "-q", "-am", "three")
    src.write_text("int keep_me(int x)\n{\n    int picked_line = 7;\n    return x;\n}\n")
    _git(
        work,
        "commit",
        "-q",
        "-am",
        "Backport #4242 to rel: keep\n\n(cherry picked from commit abcdef1234567)",
    )
    bare = tmp_path / "mirror.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return str(bare), "rel"


def _mirror_groups(removed: list[str], added: list[str], path: str = "a.cpp") -> list[pp.HunkGroup]:
    g = pp.HunkGroup(path=path, symbol="keep_me", hunks=1)
    g.removed = [pp.normalise(x) for x in removed]
    g.added = [pp.normalise(x) for x in added]
    return [g]


@pytest.mark.asyncio
async def test_tip_presence_and_line_history_read_the_real_mirror(mirror) -> None:
    git_dir, branch = mirror
    tip = await ph.tip_presence(
        git_dir,
        branch,
        "a.cpp",
        ["return x;", "int keep_me(int x)", "auto tmp = compute(x, 1);", "auto never_here = 1;"],
    )
    assert tip == {
        "return x;": True,
        "int keep_me(int x)": True,
        "auto tmp = compute(x, 1);": False,
        "auto never_here = 1;": False,
    }
    assert await ph.tip_presence(git_dir, branch, "nope.cpp", ["return x;"]) == {}
    assert await ph.tip_presence(git_dir, branch, "a.cpp", []) == {}
    never = await ph.line_history(git_dir, branch, "a.cpp", "auto never_here = 1;")
    assert never.state == "never" and never.commit == ""
    removed = await ph.line_history(git_dir, branch, "a.cpp", "auto tmp = compute(x, 1);")
    assert removed.state == "removed" and len(removed.commit) == 12  # whitespace widened


@pytest.mark.asyncio
async def test_branch_history_decides_from_the_tip_then_the_history(mirror) -> None:
    git_dir, branch = mirror
    gate = asyncio.Semaphore(2)
    # the pre-image at the tip, the fix's lines absent → gap
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(
            ["    return x;", "int keep_me(int x)"],
            ["    return x + fixed(x);", "int keep_me(int x, bool fixed)"],
        ),
        gate=gate,
    )
    assert (hp.verdict, hp.epistemic, hp.mode) == ("gap", "exact", ph.EXACT)
    assert (hp.added_present, hp.added_total, hp.removed_present, hp.removed_total) == (
        0,
        2,
        2,
        2,
    ) and not hp.lines
    # the fixed state at the tip → not_affected exact
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(
            ["    auto  tmp =   compute(x, 1);", "    auto other_gone = compute(x, 2);"],
            ["    return x;", "int keep_me(int x)"],
        ),
        gate=gate,
    )
    assert (hp.verdict, hp.epistemic, hp.reason) == ("not_affected", "exact", "fixed_at_tip")
    # neither image at the tip; the removed lines never there → restructured, by history
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(
            ["    auto never_a = 1;", "    auto never_b = 2;"],
            ["    auto also_absent = 3;", "    auto and_this = 4;"],
        ),
        gate=gate,
    )
    assert (hp.verdict, hp.epistemic, hp.reason) == ("not_affected", "lower_bound", "restructured")
    assert [x.state for x in hp.lines] == ["never", "never"]
    # a remnant of the pre-image at the tip (1 of 3), the rest never there → restructured_partial
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(
            ["    auto never_a = 1;", "    auto never_b = 2;", "    return x;"],
            ["    auto also_absent = 3;", "    auto and_this = 4;"],
        ),
        gate=gate,
    )
    assert (hp.verdict, hp.reason, hp.removed_present) == (
        "not_affected",
        "restructured_partial",
        1,
    )
    # a removed line that was there once → removed_on_branch
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(
            ["    auto  tmp =   compute(x, 1);", "    auto never_b = 2;"],
            ["    auto also_absent = 3;", "    auto and_this = 4;"],
        ),
        gate=gate,
    )
    assert hp.reason == "removed_on_branch" and sorted(x.state for x in hp.lines) == [
        "never",
        "removed",
    ]
    # the fix's added lines all at the tip: the commit that brought one names the fix → picked
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(["    auto never_a = 1;"], ["    int picked_line = 7;", "    return x;"]),
        gate=gate,
        pr_number=4242,
    )
    assert (hp.verdict, hp.epistemic, hp.reason) == ("not_affected", "exact", "picked")
    assert any(x.state == "picked" and x.subject.startswith("Backport #4242") for x in hp.lines)
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(["    auto never_a = 1;"], ["    int picked_line = 7;", "    return x;"]),
        gate=gate,
        pr_number=1,
        merge_sha="abcdef1234567",
    )
    assert hp.reason == "picked"  # the merge sha in the body names it too
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(["    auto never_a = 1;"], ["    int picked_line = 7;", "    return x;"]),
        gate=gate,
        pr_number=99,
    )
    assert hp.verdict == "" and hp.reason == "unnamed_origin"  # generic lines prove nothing
    # a genuine mix at the tip: no history read decides it
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(
            ["    auto never_a = 1;", "    return x;"],
            ["    int picked_line = 7;", "    auto and_this = 4;"],
        ),
        gate=gate,
    )
    assert hp.verdict == "" and hp.reason == "undecided" and hp.lines == []
    # every path absent at the tip → not_affected, path_absent, no history read
    hp = await ph.branch_history(
        git_dir,
        branch,
        _mirror_groups(["    return x;", "int keep_me(int x)"], ["a b c d"], path="gone.cpp"),
    )
    assert (hp.verdict, hp.reason, hp.paths_absent, hp.lines) == (
        "not_affected",
        "path_absent",
        1,
        [],
    )
    assert (
        await ph.branch_history(git_dir, "", _mirror_groups(["    return x;"], []))
    ).reason == "no_branch_ref"
    assert (await ph.branch_history(git_dir, branch, [])).reason == "no_informative_lines"


@pytest.mark.asyncio
async def test_a_ref_or_mirror_that_is_not_there_is_never_path_absent(mirror) -> None:
    """A branch the mirror has not fetched, or no mirror at all, reads nothing: no
    verdict, never `not_affected` for paths nobody looked at."""
    git_dir, branch = mirror
    groups = _mirror_groups(["    return x;", "int keep_me(int x)"], ["a b c d"])
    for gd, br in [(git_dir, "no-such-branch"), ("/nonexistent/mirror.git", branch)]:
        hp = await ph.branch_history(gd, br, groups)
        assert (hp.verdict, hp.reason, hp.paths_absent) == ("", "no_branch_ref", 0), (gd, br)


@pytest.mark.asyncio
async def test_a_deadline_on_the_mirror_is_never_a_verdict(mirror, monkeypatch) -> None:
    git_dir, branch = mirror

    async def _slow(*a, **k):
        return -1, ""

    monkeypatch.setattr(ph, "_git_rc", _slow)
    assert await ph.tip_presence(git_dir, branch, "a.cpp", ["return x;"]) is None
    hp = await ph.branch_history(
        git_dir, branch, _mirror_groups(["    return x;", "int keep_me(int x)"], [])
    )
    assert hp.verdict == "" and hp.reason == "deadline"
    assert (await ph.line_history(git_dir, branch, "a.cpp", "return x;")).state == "deadline"


@pytest.mark.asyncio
async def test_a_cancelled_git_read_kills_its_child(monkeypatch) -> None:
    """code_backport cancelling the leg must not leave `git log -S` running."""
    started = asyncio.Event()

    class _Proc:
        returncode: int | None = None
        killed = False

        async def communicate(self):
            started.set()
            await asyncio.sleep(3600)

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return -9

    proc = _Proc()

    async def _exec(*a, **k):
        return proc

    monkeypatch.setattr(ph.asyncio, "create_subprocess_exec", _exec)
    task = asyncio.create_task(ph._git_rc("/m.git", "log", "-S", "x", timeout_s=60))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


# ---------------------------------------------------------------- the handler


class _Repo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def pr_facts(self, org, ns, n):
        return {
            "pr_number": n,
            "title": "Fix",
            "labels": ["pr-must-backport"],
            "files": [SRC],
            "merge_commit_sha": b"e" * 40,
            "merged_at": datetime(2026, 9, 15),
            "body_excerpt": "",
        }

    async def backport_prs_titled(self, org, ns, n, *, limit=8):
        return []

    async def symbol_names_present(self, org, ns, names):
        return {n: [n] for n in names}

    async def live_paths_present(self, org, ns, files):
        return set(files)

    async def chunks_for_patch(self, org, ns, paths, symbols, needles, *, limit=400):
        self.calls.append(("patch", ns))
        return [
            {
                "path": SRC,
                "scope": "DB::IDataType::getSubcolumnInfo",
                "content": "nothing of either image\n",
            }
        ]


class _Source:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def pr_change(self, org, ns, n):
        self.calls.append("pr_change")
        return {
            "repo": "ClickHouse/ClickHouse",
            "pr_number": n,
            "title": "Fix",
            "url": "",
            "author": "a",
            "base_branch": "master",
            "minor_version": "26.9",
            "is_merged": 1,
            "is_backport_pr": 0,
            "is_cherrypick_pr": 0,
            "is_reverted": 0,
            "upstream_pr_number": 0,
            "backport_required": 1,
            "changelog_category": "Bug Fix",
            "src_components": [],
            "files": [SRC],
            "merged_at": datetime(2026, 9, 15),
            "hunk_contexts": ["void IDataType::getSubcolumnInfo("],
        }

    async def backport_prs_of(self, org, ns, n):
        return []

    async def pr_diff_sections(self, org, ns, n):
        self.calls.append("pr_diff_sections")
        return [
            f"a/{SRC} b/{SRC}\n--- a/{SRC}\n+++ b/{SRC}\n@@ -1,4 +1,4 @@ void IDataType::getSubcolumnInfo(\n"
            "-    auto name = ISerialization::getSubcolumnNameForStream(subpath, prefix_len, false);\n"
            "-    if (name == requested_name && !cache.contains(key))\n"
            "-    return makeSubcolumnInfo(subpath, prefix_len);\n"
            "+    auto cache_key = SubstreamsCacheKey::fromPath(subpath, prefix_len);\n"
            "+    if (auto cached = substreams_cache.lookupElement(cache_key))\n"
            "+    return cached->subcolumn_info;\n"
        ]


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import server.code.app.search_fanout as fanout
    import server.code.domain.index.repos as repos
    import server.tracker.store.source as qs

    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [_Cfg(NS, "master"), *(_Cfg(n, v) for n, v in BRANCHES.items())],
    )
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    async def _authorized(org, code_ns, *, hides_answer=True):
        return QA

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)

    async def _as_of(repo, org, ns):
        return {"commit": f"{ns}-head", "status": "fresh"}

    monkeypatch.setattr(h, "index_as_of", _as_of)
    authz: list[list[str]] = []

    async def _authorize(org, namespaces, *, op="code.search"):
        authz.append(list(namespaces))

    monkeypatch.setattr(fanout, "authorize", _authorize)
    holder: dict[str, Any] = {"authz": authz, "src": _Source(), "repo": _Repo(), "history": []}
    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])
    monkeypatch.setattr(h, "CodeRepo", lambda: holder["repo"])

    async def _branch_history(git_dir, branch, groups, *, timeout_s=2.0, gate=None, **kw):
        holder["fix_ref"] = (kw.get("pr_number"), kw.get("merge_sha"))
        holder["history"].append((git_dir, branch, [g.path for g in groups]))
        if branch == "26.6":
            return _tip((0, 3), (3, 3))  # the pre-image at the tip
        if branch == "26.5":
            return _hist("never", "never", "never")
        if branch == "26.4":
            return _tip((1, 3), (0, 3))  # neither image, the history did not run: undecided
        return _hist("removed", "never", "never", commits=("abc123", "", ""))

    monkeypatch.setattr(ph, "branch_history", _branch_history)
    return holder


@pytest.mark.asyncio
async def test_history_unset_is_byte_identical_to_exact_gaps(wired) -> None:
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, exact=True)
    )
    assert set(out.stage_ms) == {"fix", "presence", "patch"} and wired["history"] == []
    assert all("history" not in b["presence"] for b in out.branches)
    assert all(b["basis"] != "history" for b in out.branches)
    assert all(b["presence"]["patch"]["reason"] == "undecided" for b in out.branches)
    assert CodeBackportGapInput(org=ORG, namespace=NS).history is False
    assert CodeBackportGapInput(org=ORG, namespace=NS).exact is False


@pytest.mark.asyncio
async def test_history_runs_for_the_undecided_branches_and_decides_them(wired) -> None:
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(org=ORG, namespace=NS, pr_number=115670, history=True)
    )
    assert wired["src"].calls.count("pr_diff_sections") == 1  # history implies exact
    assert set(out.stage_ms) == {"fix", "presence", "patch", "history"}
    assert sorted(br for _, br, _ in wired["history"]) == sorted(BRANCHES.values())
    assert all(gd == "var/code_repos/clickhouse.git" for gd, _, _ in wired["history"])
    assert wired["history"][0][2] == [SRC]  # the fix's hunk groups, by path
    by = {b["branch"]: b for b in out.branches}
    assert (by["26.6"]["verdict"], by["26.6"]["epistemic"], by["26.6"]["basis"]) == (
        "gap",
        "exact",
        "history",
    )
    assert by["26.6"]["cherry_pick"] == {"pr_number": 115670, "merge_sha": "e" * 40}
    assert (
        by["26.5"]["verdict"],
        by["26.5"]["epistemic"],
        by["26.5"]["basis"],
        by["26.5"]["reason"],
    ) == ("not_affected", "lower_bound", "history", "restructured")
    assert (
        by["26.4"]["verdict"] == "gap" and by["26.4"]["basis"] == "symbol"
    )  # mixed: the floor stands
    assert by["26.4"]["presence"]["history"]["reason"] == "undecided"
    assert (by["26.3"]["verdict"], by["26.3"]["reason"]) == ("not_affected", "removed_on_branch")
    assert by["26.6"]["presence"]["history"]["removed_present"] == 3
    assert (
        len(wired["authz"]) == 1
    )  # the symbol leg authorized once; history adds no gate of its own
    assert wired["fix_ref"] == (115670, "e" * 40)  # the leg knows which fix a pick would name


@pytest.mark.asyncio
async def test_history_deadline_and_error_leave_the_floor(wired, monkeypatch) -> None:
    async def _hang(git_dir, branch, groups, *, timeout_s=2.0, gate=None, **kw):
        if branch == "26.6":
            raise RuntimeError("boom")
        await asyncio.sleep(5)
        return ph.HistoryPresence()

    monkeypatch.setattr(ph, "branch_history", _hang)
    monkeypatch.setattr(settings, "code_backport_history_budget_ms", 50)
    out = await h.handle_code_backport_gap(
        CodeBackportGapInput(
            org=ORG, namespace=NS, pr_number=115670, history=True, branches=["26.6", "26.5"]
        )
    )
    by = {b["branch"]: b for b in out.branches}
    assert by["26.6"]["presence"]["history"]["reason"] == "error:RuntimeError"
    assert by["26.5"]["presence"]["history"]["reason"] == "deadline"
    assert all(b["basis"] == "symbol" and b["epistemic"] == "lower_bound" for b in out.branches)


def test_branch_refs_come_from_the_configuration(monkeypatch) -> None:
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [
            _Cfg(NS, "master"),
            _Cfg("code-clickhouse-26-6", "26.6"),
            _Cfg("other", "main", repo="x/y"),
        ],
    )
    git_dir, refs = h.branch_refs(ORG, NS)
    assert git_dir == "var/code_repos/clickhouse.git" and refs == {"code-clickhouse-26-6": "26.6"}
    assert h.branch_refs(ORG, "unknown") == ("", {})


# ---------------------------------------------------------------- the worker


class _GapsRepo:
    def __init__(self, stamps: dict[int, gr.FixStamp] | None = None) -> None:
        self.stamps = stamps or {}
        self.inserted: list[list[dict[str, Any]]] = []

    async def fix_stamps(self, org, ns):
        return dict(self.stamps)

    async def insert_rows(self, rows):
        for r in rows:
            gr.build_gap_row(**r)  # every column present, no unknown key
        self.inserted.append(list(rows))


class _CodeRepo:
    def __init__(self, prs: list[int], head: str = "a" * 40) -> None:
        self.prs, self.head = prs, head

    async def get_state(self, org, ns, key):
        return self.head if key == "git_head" else ""

    async def labelled_fixes_in_window(
        self, org, ns, *, since, label_prefix="pr-must-backport", limit=2000
    ):
        return [
            {
                "pr_number": n,
                "title": f"Fix {n}",
                "merged_at": NOW - timedelta(days=i),
                "labels": ["pr-must-backport", f"v26.{6 - (i % 2)}-must-backport"],
                "merge_commit_sha": b"\xaa" * 20,
            }
            for i, n in enumerate(self.prs)
        ]


class _WantedSource:
    def __init__(self, nums: list[int], fail: bool = False) -> None:
        self.nums, self.fail = nums, fail

    async def backport_wanted_prs(self, org, ns, *, since):
        from server.tracker.domain.errors import TrackerUnavailable

        if self.fail:
            raise TrackerUnavailable("down")
        return list(self.nums)


@dataclass
class _Out:
    branches: list[dict[str, Any]]
    fix: dict[str, Any]
    causes: list[str]
    tracker_unavailable: bool = False


def _out(pr: int, *, unavailable: bool = False) -> _Out:
    rows = []
    for ns, v in BRANCHES.items():
        verdict, ep, basis, reason, patch = "gap", "exact", "hunk", "", {"mode": "exact"}
        if v == "26.8":
            verdict, basis = "covered", "backport_pr"
        elif v == "26.7":
            verdict, ep, basis, patch = (
                "gap",
                "lower_bound",
                "symbol",
                {"mode": "symbol_only", "reason": "undecided"},
            )
        elif v == "26.6":
            basis = "history"
        rows.append(
            {
                "namespace": ns,
                "branch": v,
                "verdict": verdict,
                "epistemic": ep,
                "basis": basis,
                "reason": reason,
                "cherry_pick": {"pr_number": pr, "merge_sha": "e" * 40} if verdict == "gap" else {},
                "backport": {"pr_number": 120301} if verdict == "covered" else {},
                "presence": {"patch": patch},
            }
        )
    return _Out(
        branches=rows,
        fix={"minor_version": "26.9", "title": f"Fix {pr}", "merged_at": "2026-09-15T00:00:00"},
        causes=["tracker_unavailable"] if unavailable else [],
        tracker_unavailable=unavailable,
    )


@pytest.fixture
def worker_cfg(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import server.code.domain.index.repos as repos

    cfgs = [
        _Cfg(NS, "master"),
        *(_Cfg(n, v) for n, v in BRANCHES.items()),
        _Cfg(
            "code-example-private",
            "master",
            repo="ClickHouse/example-private",
            classification="restricted",
        ),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: cfgs)

    async def _authorized(org, code_ns, *, hides_answer=True):
        return QA

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    monkeypatch.setattr(settings, "code_backport_gaps_pace_s", 0.0, raising=False)
    monkeypatch.setattr(settings, "code_backport_gaps_prs_per_pass", 300, raising=False)
    monkeypatch.setattr(settings, "code_backport_gaps_budget_s", 1800.0, raising=False)
    monkeypatch.setattr(settings, "code_backport_gaps_days", 90, raising=False)
    return cfgs


def test_trunks_are_standard_masters_with_release_siblings(worker_cfg) -> None:
    got = wk.trunks()
    assert [c.namespace for c, _ in got] == [NS]
    assert sorted(s.namespace for s in got[0][1]) == sorted(
        BRANCHES
    )  # the restricted trunk is skipped


def test_order_fixes_puts_never_computed_first_then_stalest_and_skips_settled() -> None:
    fixes = {
        1: {"merged_at": NOW - timedelta(days=3)},
        2: {"merged_at": NOW},
        3: {"merged_at": NOW - timedelta(days=1)},
        4: {"merged_at": NOW},
        5: {"merged_at": NOW},
    }
    stamps = {
        1: gr.FixStamp(latest=NOW - timedelta(days=2), settled=False, rows=8),
        3: gr.FixStamp(latest=NOW - timedelta(days=5), settled=False, rows=8),
        4: gr.FixStamp(latest=NOW - timedelta(days=1), settled=True, rows=8),
        5: gr.FixStamp(latest=NOW - timedelta(days=1), settled=True, rows=3),
    }  # settled but not every branch
    todo, settled = wk.order_fixes(fixes, stamps, branch_count=8)
    assert todo == [2, 3, 1, 5] and settled == 1


@pytest.mark.asyncio
async def test_pass_computes_labelled_fixes_writes_rows_in_batches_and_counts(worker_cfg) -> None:
    calls: list[Any] = []

    async def _runner(req):
        calls.append(req)
        return _out(req.pr_number, unavailable=req.pr_number == 103)

    repo = _GapsRepo(
        stamps={101: gr.FixStamp(latest=NOW - timedelta(days=1), settled=True, rows=8)}
    )
    result = wk.BackportGapsResult()
    await wk._run(
        result,
        now=NOW,
        repo=repo,
        code_repo=_CodeRepo([101, 102, 103, 104]),
        source_factory=lambda org, ns: _WantedSource([104, 105]),
        runner=_runner,
    )
    assert result.namespaces == 1 and result.fixes_seen == 5 and result.fixes_settled == 1
    assert [r.pr_number for r in calls] == [
        102,
        103,
        104,
        105,
    ]  # never computed, newest merge first; QA-only last
    assert all(r.exact and r.history and not r.trace and r.consumer == wk.CONSUMER for r in calls)
    assert (
        result.fixes_computed == 3 and result.fixes_unavailable == 1
    )  # #103's record was down: nothing written
    assert result.rows_written == 24 and len(repo.inserted) == 1  # one batch at the end
    rows = repo.inserted[0]
    assert {r["pr_number"] for r in rows} == {102, 104, 105}
    by = {(r["pr_number"], r["branch"]): r for r in rows}
    assert by[(102, "26.6")]["basis"] == "history" and by[(102, "26.6")]["verdict"] == "gap"
    assert by[(102, "26.8")]["settled"] == 1 and by[(102, "26.8")]["backport_pr"] == 120301
    assert by[(102, "26.7")]["settled"] == 0 and by[(102, "26.7")]["epistemic"] == "lower_bound"
    assert (
        by[(102, "26.5")]["cherry_pick_pr"] == 102
        and by[(102, "26.5")]["cherry_pick_sha"] == "e" * 40
    )
    assert (
        by[(104, "26.6")]["wanted_source"] == "label+tracker"
        and by[(105, "26.6")]["wanted_source"] == "tracker"
    )
    # #102 is the second ledger row (i=1) → labelled v26.5-must-backport
    assert by[(102, "26.5")]["wanted_branch"] == 1 and by[(102, "26.6")]["wanted_branch"] == 0
    assert by[(105, "26.6")]["pr_merged_at"] == datetime(
        2026, 9, 15
    )  # from the answer when the ledger has none
    assert all(
        r["computed_at"] == NOW and r["version"] == rows[0]["version"] and r["label_wanted"] == 1
        for r in rows
    )
    out = result.outcome()
    assert out["rows_gap"] == 21 and out["rows_history"] == 3 and out["rows_undecided"] == 3
    assert out["statements"] == 4 and out["partial"] is False and out["errors"] == 0
    assert set(out) >= {
        "fixes_seen",
        "fixes_computed",
        "fixes_settled",
        "fixes_deferred",
        "fixes_unavailable",
        "rows_written",
        "wall_ms",
        "resume",
    }


@pytest.mark.asyncio
async def test_a_trunk_before_its_first_index_computes_nothing(worker_cfg) -> None:
    calls: list[Any] = []

    async def _runner(req):
        calls.append(req)
        return _out(req.pr_number)

    repo = _GapsRepo(stamps={})
    result = wk.BackportGapsResult()
    await wk._run(
        result,
        now=NOW,
        repo=repo,
        code_repo=_CodeRepo([101], head=""),
        source_factory=lambda org, ns: _WantedSource([105]),
        runner=_runner,
    )
    assert calls == [] and repo.inserted == [] and result.namespaces == 0


@pytest.mark.asyncio
async def test_pass_caps_and_budgets_and_a_failing_fix_does_not_end_it(
    worker_cfg, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "code_backport_gaps_prs_per_pass", 2, raising=False)
    seen: list[int] = []

    async def _runner(req):
        seen.append(req.pr_number)
        if req.pr_number == 202:
            raise RuntimeError("boom")
        return _out(req.pr_number)

    repo = _GapsRepo()
    result = wk.BackportGapsResult()
    await wk._run(
        result,
        now=NOW,
        repo=repo,
        code_repo=_CodeRepo([201, 202, 203, 204]),
        source_factory=lambda org, ns: _WantedSource([], fail=True),
        runner=_runner,
    )
    assert seen == [201, 202] and result.fixes_deferred == 2 and result.partial is True
    assert (
        result.resume == f"{NS}:#203"
        and result.fixes_unavailable == 1
        and result.fixes_computed == 1
    )
    assert any("backport_required unavailable" in e for e in result.errors)
    monkeypatch.setattr(settings, "code_backport_gaps_prs_per_pass", 300, raising=False)
    monkeypatch.setattr(settings, "code_backport_gaps_budget_s", -1.0, raising=False)
    result = wk.BackportGapsResult()
    await wk._run(
        result,
        now=NOW,
        repo=_GapsRepo(),
        code_repo=_CodeRepo([201]),
        source_factory=lambda org, ns: _WantedSource([]),
        runner=_runner,
    )
    assert (
        result.fixes_deferred == 1 and result.fixes_computed == 0 and result.resume == f"{NS}:#201"
    )


@pytest.mark.asyncio
async def test_rows_are_inserted_every_fifty_fixes(worker_cfg) -> None:
    async def _runner(req):
        return _out(req.pr_number)

    repo = _GapsRepo()
    result = wk.BackportGapsResult()
    await wk._run(
        result,
        now=NOW,
        repo=repo,
        code_repo=_CodeRepo(list(range(300, 375))),
        source_factory=lambda org, ns: _WantedSource([]),
        runner=_runner,
    )
    assert [len(b) for b in repo.inserted] == [400, 200] and result.rows_written == 600


def test_worker_is_registered_with_its_own_deadline(monkeypatch) -> None:
    from server.workers import registry

    monkeypatch.setattr(settings, "code_backport_gaps_enabled", True, raising=False)
    monkeypatch.setattr(settings, "code_pr_history_enabled", True, raising=False)
    specs = {s.name: s for s in registry.enabled_workers()}
    assert (
        specs["code_backport_gaps"].max_pass_seconds == settings.code_backport_gaps_max_pass_seconds
    )
    assert specs["code_backport_gaps"].interval_seconds == 86400.0
    monkeypatch.setattr(settings, "code_pr_history_enabled", False, raising=False)
    # no ledger, nothing to read
    assert "code_backport_gaps" not in {s.name for s in registry.enabled_workers()}
    assert "code_backport_gaps" in registry.worker_meta()


# ---------------------------------------------------------------- the read op


def _row(
    pr: int, branch: str, verdict: str, *, wanted_branch: int = 0, ns: str = ""
) -> dict[str, Any]:
    return gr.build_gap_row(
        org=ORG,
        code_namespace=NS,
        branch_namespace=ns or f"code-clickhouse-{branch.replace('.', '-')}",
        pr_number=pr,
        branch=branch,
        verdict=verdict,
        epistemic="exact" if verdict != "unknown" else "lower_bound",
        basis="hunk" if verdict == "gap" else "backport_pr",
        cherry_pick_pr=pr if verdict == "gap" else 0,
        cherry_pick_sha="e" * 40 if verdict == "gap" else "",
        backport_pr=7 if verdict == "covered" else 0,
        label_wanted=1,
        wanted_branch=wanted_branch,
        wanted_source="label",
        settled=int(verdict == "covered"),
        pr_title=f"Fix {pr}",
        pr_merged_at=datetime(2026, 9, 10),
        pr_minor_version="26.9",
        computed_at=NOW - timedelta(hours=3),
        version=1,
    )


class _ReadRepo:
    def __init__(
        self, rows: list[dict[str, Any]], last: datetime | None = NOW - timedelta(hours=3)
    ) -> None:
        self.rows, self.last, self.calls = rows, last, []

    async def rows_for_branch(self, org, ns, branch_ns, *, since, wanted_only=True, limit=5000):
        self.calls.append(("branch", branch_ns, wanted_only, limit))
        return [r for r in self.rows if r["branch_namespace"] == branch_ns]

    async def rows_for_namespace(
        self, org, ns, *, since, wanted_only=True, limit=5000, branches=None
    ):
        self.calls.append(("namespace", wanted_only, limit))
        self.branches = branches
        return [r for r in self.rows if branches is None or r["branch_namespace"] in branches]

    async def namespace_stats(self, org, ns):
        return gr.GapStats(
            fixes=3, rows=len(self.rows), last_computed=self.last, first_computed=self.last
        )


@pytest.fixture
def read_wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import server.code.app.search_fanout as fanout
    import server.code.domain.index.repos as repos

    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [_Cfg(NS, "master"), *(_Cfg(n, v) for n, v in BRANCHES.items())],
    )
    monkeypatch.setattr(hg, "require_code_intel", lambda: None)

    # The handler judges `stale` against the wall clock and the fixture rows
    # carry fixed stamps, so a real clock would flip `stale` as time passes.
    # Judge against NOW instead.
    real_is_stale = hg.is_stale
    monkeypatch.setattr(hg, "is_stale", lambda last, *, now: real_is_stale(last, now=NOW))
    holder: dict[str, Any] = {
        "authz": [],
        "repo": _ReadRepo(
            [
                _row(10, "26.6", "gap"),
                _row(11, "26.6", "gap", wanted_branch=1),
                _row(12, "26.6", "covered"),
                _row(10, "26.5", "gap"),
                _row(9, "26.6", "unknown"),
                _row(8, "26.6", "gap", ns="code-clickhouse-25-9"),
            ]
        ),
    }

    async def _authorize(org, namespaces, *, op="code.search"):
        holder["authz"].append((op, list(namespaces)))
        if "code-clickhouse-26-1" in namespaces and holder.get("refuse"):
            raise AuthInsufficientError("no")

    monkeypatch.setattr(fanout, "authorize", _authorize)
    monkeypatch.setattr(hg, "CodeBackportGapsRepo", lambda: holder["repo"])
    return holder


@pytest.mark.asyncio
async def test_report_for_one_branch_ranks_gaps_then_wanted_then_newest(read_wired) -> None:
    out = await hg.handle_code_backport_gaps(
        CodeBackportGapsInput(org=ORG, namespace=NS, branch="26.6")
    )
    assert out.branch == "26.6" and out.branch_namespace == "code-clickhouse-26-6"
    assert read_wired["authz"] == [("code.backport_gaps", ["code-clickhouse-26-6"])]
    assert read_wired["repo"].calls == [("branch", "code-clickhouse-26-6", True, 500)]
    assert [(r["pr_number"], r["verdict"]) for r in out.rows] == [
        (11, "gap"),
        (10, "gap"),
        (9, "unknown"),
        (12, "covered"),
    ]
    assert out.rows[0]["wanted_branch"] is True and out.rows[0]["cherry_pick"] == {
        "pr_number": 11,
        "merge_sha": "e" * 40,
    }
    assert (
        out.rows[3]["backport_pr"] == 7
        and out.rows[3]["settled"] is True
        and out.rows[3]["cherry_pick"] == {}
    )
    assert out.counts == {"gap": 2, "pending": 0, "unknown": 1, "not_affected": 0, "covered": 1}
    assert out.counts_by_branch == {"26.6": {"gap": 2, "unknown": 1, "covered": 1}}
    assert (
        out.fixes == 4
        and out.stale is False
        and out.computed["rows"] == 6
        and out.computed["fixes"] == 3
    )
    assert (
        out.rows[0]["computed_at"].startswith("2026-09-16T09:00")
        and out.rows[0]["merged_at"] == "2026-09-10T00:00:00"
    )


@pytest.mark.asyncio
async def test_report_for_every_branch_serves_configured_branches_only_and_says_stale(
    read_wired,
) -> None:
    read_wired["repo"].last = None
    out = await hg.handle_code_backport_gaps(
        CodeBackportGapsInput(org=ORG, namespace=NS, wanted_only=False, limit=3)
    )
    assert out.branch == "" and out.branches == [f"code-clickhouse-26-{i}" for i in range(8, 0, -1)]
    assert read_wired["repo"].calls == [("namespace", False, 3)]
    assert len(read_wired["authz"][0][1]) == 8
    # the branch set narrows the read itself, before its LIMIT: rows of an unconfigured
    # or unreadable branch never use up the page
    assert read_wired["repo"].branches == [f"code-clickhouse-26-{i}" for i in range(8, 0, -1)]
    assert [(r["pr_number"], r["branch"]) for r in out.rows] == [
        (11, "26.6"),
        (10, "26.6"),
        (10, "26.5"),
    ]
    assert all(
        r["branch"] != "" and r["pr_number"] != 8 for r in out.rows
    )  # 25.9 is not configured
    assert out.stale is True and out.computed["last_computed"] == ""
    assert hg.is_stale(NOW - timedelta(days=3), now=NOW) is True
    assert hg.is_stale(NOW - timedelta(days=1), now=NOW) is False


@pytest.mark.asyncio
async def test_report_fails_closed_on_a_refused_branch(read_wired) -> None:
    read_wired["refuse"] = True
    with pytest.raises(AuthInsufficientError):
        await hg.handle_code_backport_gaps(CodeBackportGapsInput(org=ORG, namespace=NS))
    assert read_wired["repo"].calls == []


def test_op_is_registered_for_viewers_with_defaults_that_need_no_argument() -> None:
    op = OPERATIONS_BY_NAME["code.backport_gaps"]
    assert op.required_role == Role.VIEWER and op.idempotent and op.maturity == "experimental"
    inp = CodeBackportGapsInput()
    assert (inp.branch, inp.wanted_only, inp.days, inp.limit) == ("", True, 90, 500)
    assert CodeBackportGapInput().history is False


def test_build_gap_row_fills_every_column_and_rejects_drift() -> None:
    row = gr.build_gap_row(
        org=ORG, code_namespace=NS, branch_namespace="code-clickhouse-26-6", pr_number=1
    )
    assert (
        set(row) == set(gr.GAP_COLS)
        and row["pr_merged_at"] == datetime(1970, 1, 1)
        and row["version"] > 0
    )
    with pytest.raises(ValueError, match="unknown columns"):
        gr.build_gap_row(nope=1)


# ---------------------------------------------------------------- the SDK


@pytest.mark.asyncio
async def test_sdk_bodies() -> None:
    from sdk.python.loom.code import CodeClient

    class _Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def post(self, path, body):
            self.calls.append((path, body))
            return {}

    class _Agent:
        def __init__(self) -> None:
            self.transport = _Transport()

    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.backport_gaps("26.6")
    await c.backport_gaps()
    await c.backport_gap(115670, history=True)
    await c.backport_gap(115670)
    assert ag.transport.calls[0] == (
        "/v1/code.backport_gaps",
        {"wanted_only": True, "days": 90, "limit": 500, "branch": "26.6"},
    )
    assert ag.transport.calls[1][1] == {"wanted_only": True, "days": 90, "limit": 500}
    assert ag.transport.calls[2][1] == {"max_symbols": 8, "pr_number": 115670, "history": True}
    assert "history" not in ag.transport.calls[3][1] and "exact" not in ag.transport.calls[3][1]


@pytest.mark.asyncio
async def test_a_failed_git_read_is_never_every_line_absent(mirror, monkeypatch) -> None:
    """`git grep` exits 1 for a clean miss and 128 when it could not read the
    file; the second is an error the leg reports, not a tip without the fix's
    lines (which the removed-line history reads as `removed_on_branch`)."""
    git_dir, branch = mirror
    real = ph._git_rc

    async def _grep_fails(gd, *args, timeout_s):
        if args and args[0] == "grep":
            return 128, ""
        return await real(gd, *args, timeout_s=timeout_s)

    monkeypatch.setattr(ph, "_git_rc", _grep_fails)
    with pytest.raises(ph.MirrorReadError):
        await ph.tip_presence(git_dir, branch, "a.cpp", ["return x;"])
    hp = await ph.branch_history(
        git_dir, branch, _mirror_groups(["    return x;", "int keep_me(int x)"], [])
    )
    assert hp.verdict == "" and hp.reason == "error:MirrorReadError"


@pytest.mark.asyncio
async def test_a_failed_path_listing_is_not_a_missing_path(mirror, monkeypatch) -> None:
    git_dir, branch = mirror
    real = ph._git_rc

    async def _ls_fails(gd, *args, timeout_s):
        if args and args[0] == "ls-tree":
            return 128, ""
        return await real(gd, *args, timeout_s=timeout_s)

    monkeypatch.setattr(ph, "_git_rc", _ls_fails)
    hp = await ph.branch_history(git_dir, branch, _mirror_groups(["    return x;"], []))
    assert hp.verdict == "" and hp.reason == "error:MirrorReadError" and hp.paths_absent == 0
