"""Every entry's mirror is fetched every pass, one fetch per
mirror; the pass's commits run oldest-first across entries; a commit that
cannot finish before the deadline waits for the next pass; and `as_of` says
stale when the mirror has not fetched.

Lag counted against a mirror the pass never fetched would report
`lag_commits: 0, status: fresh` for an index that is behind, and one entry
draining its whole backlog in its turn can run the pass into its timeout."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app import common as cc
from server.code.app.index import gitrepo
from server.code.app.index import ingest_pass as w
from server.code.app.index import pass_budget as ingest_budget
from server.code.app.index import pass_commit as ingest_commit
from server.code.app.index import pass_fetch as ingest_fetch
from server.code.app.index import pass_stream as ingest_stream
from server.settings import settings
from tests.unit.test_code_ingest_pass_budget import _cfg, _Clock, _StateRepo, _wire_commits

pytestmark = pytest.mark.usefixtures("mirror_here")


@pytest.fixture(autouse=True)
def _locks_in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The fake git_dirs are relative: their fetch-lock files land in tmp_path."""
    monkeypatch.chdir(tmp_path)


def _entry(ns: str, git_dir: str, branch: str) -> Any:
    return _cfg(ns, git_dir=git_dir, git_branch=branch, git_remote="r", github_token="")


# ---------------------------------------------------------------------------
# one fetch per mirror
# ---------------------------------------------------------------------------


def test_a_shared_mirror_is_fetched_once_for_all_its_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fetch_many(git_dir: str, remote: str, branches: list[str], token: str = "") -> None:
        calls.append((git_dir, tuple(branches)))

    monkeypatch.setattr(gitrepo, "fetch_many", fetch_many)
    monkeypatch.setattr("os.path.isdir", lambda d: True)
    repos = [
        _entry("code-a", "pub.git", "master"),
        _entry("code-a-26-1", "pub.git", "26.1"),
        _entry("code-a-26-2", "pub.git", "26.2"),
        _entry("code-solo", "solo.git", "main"),
    ]
    result = ingest_budget.CodeIngestResult()
    assert asyncio.run(ingest_fetch.prefetch_mirrors(repos, result)) == 1
    assert calls == [("pub.git", ("master", "26.1", "26.2"))], (
        "the lone mirror fetches in its own turn"
    )
    assert ingest_fetch._FETCHED_THIS_PASS == {
        ("pub.git", "master"),
        ("pub.git", "26.1"),
        ("pub.git", "26.2"),
    }


def test_a_failed_batch_leaves_each_entry_its_own_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> None:
        raise gitrepo.GitError("remote hung up")

    monkeypatch.setattr(gitrepo, "fetch_many", boom)
    monkeypatch.setattr("os.path.isdir", lambda d: True)
    result = ingest_budget.CodeIngestResult()
    repos = [_entry("code-a", "pub.git", "master"), _entry("code-b", "pub.git", "26.1")]
    assert asyncio.run(ingest_fetch.prefetch_mirrors(repos, result)) == 0
    assert ingest_fetch._FETCHED_THIS_PASS == set() and "mirror fetch pub.git" in result.errors[0]


def test_fetch_many_is_one_git_fetch_with_a_refspec_per_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []

    async def git(git_dir: str, *args: str, **kw: Any) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(gitrepo, "_git", git)
    asyncio.run(gitrepo.fetch_many("m.git", "r", ["26.1", "master", "26.1", ""]))
    assert seen == [
        (
            "fetch",
            "--prune",
            "r",
            "+refs/heads/26.1:refs/heads/26.1",
            "+refs/heads/master:refs/heads/master",
        )
    ]


# ---------------------------------------------------------------------------
# oldest first, across entries
# ---------------------------------------------------------------------------


def _wire_streams(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    times: dict[str, int],
    files: dict[str, int] | None = None,
    cost: float = 10.0,
) -> list[str]:
    ingested = _wire_commits(monkeypatch, clock, cost=cost)

    async def meta(git_dir: str, shas: list[str]) -> dict[str, tuple[str, int]]:
        return {s: ("", times[s]) for s in shas if s in times}

    async def names(git_dir: str, base: str, sha: str, limit: int = 2000) -> list[str]:
        return ["f"] * (files or {}).get(sha, 1)

    monkeypatch.setattr(gitrepo, "commit_meta_many", meta)
    monkeypatch.setattr(gitrepo, "diff_name_only", names)
    return ingested


def _plan(ns: str, pending: list[str]) -> tuple[ingest_budget.RepoPass, _StateRepo]:
    repo = _StateRepo()
    plan = ingest_budget.RepoPass(_cfg(ns), repo, pending[-1], "w0", False, walk=True)
    plan.pending = list(pending)
    return plan, repo


def test_the_commit_that_waited_longest_goes_first_whatever_entry_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release tree's backlog of twenty newer commits does not hold the
    phase ahead of another entry's one older merge; each entry's own commits
    still land oldest first (the watermark contract)."""
    clock = _Clock()
    now = int(clock.time())
    backlog = [f"p{i:02d}" for i in range(20)]
    times = {sha: now - 600 + i for i, sha in enumerate(backlog)} | {"loom1": now - 3000}
    ingested = _wire_streams(monkeypatch, clock, times)
    priv, priv_repo = _plan("code-private", backlog)
    loom, loom_repo = _plan("code-loom", ["loom1"])
    result = ingest_budget.CodeIngestResult()
    budget = ingest_budget.PassBudget(started=clock.now, stop_after=clock.now + 1000.0)
    asyncio.run(ingest_stream.ingest_oldest_first([priv, loom], result, budget))
    assert ingested[0] == "loom1" and ingested[1:] == backlog
    assert loom_repo.state["git_head"] == "loom1" and priv_repo.state["git_head"] == backlog[-1]
    assert (priv.commits, loom.commits) == (20, 1) and result.commits_planned == 21


def test_the_pass_stops_between_commits_and_the_next_pass_continues_from_the_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    now = int(clock.time())
    commits = ["c1", "c2", "c3", "c4"]
    ingested = _wire_streams(
        monkeypatch, clock, {c: now - 100 + i for i, c in enumerate(commits)}, cost=100.0
    )
    plan, repo = _plan("code-a", commits)
    result = ingest_budget.CodeIngestResult()
    budget = ingest_budget.PassBudget(started=clock.now, stop_after=clock.now + 250.0)
    asyncio.run(ingest_stream.ingest_oldest_first([plan], result, budget))
    # checked before each: 0, 100, 200 start; 300 does not
    assert ingested == commits[:3]
    assert result.commits_planned == 4 and result.commits_done == 3
    assert repo.state["git_head"] == commits[2], "the watermark is the resume point"
    assert result.partial and result.resume == "code-a:commits@c3+1"

    # the next pass: what the watermark says, with a fresh budget
    ingested.clear()
    nxt_plan = ingest_budget.RepoPass(
        plan.cfg, repo, commits[-1], repo.state["git_head"], False, walk=True
    )
    nxt_plan.pending = commits[3:]
    nxt = ingest_budget.CodeIngestResult()
    budget = ingest_budget.PassBudget(started=clock.now, stop_after=clock.now + 1000.0)
    asyncio.run(ingest_stream.ingest_oldest_first([nxt_plan], nxt, budget))
    assert ingested == commits[3:] and nxt.partial is False
    assert repo.state["git_head"] == commits[3]


def test_at_the_budget_every_waiting_entry_is_cut_the_longest_waiting_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    now = int(clock.time())
    times = {"a1": now - 50, "a2": now - 40, "b1": now - 45, "b2": now - 10}
    ingested = _wire_streams(monkeypatch, clock, times, cost=100.0)
    a, _ = _plan("code-a", ["a1", "a2"])
    b, _ = _plan("code-b", ["b1", "b2"])
    result = ingest_budget.CodeIngestResult()
    budget = ingest_budget.PassBudget(
        started=clock.now, stop_after=clock.now + 150.0
    )  # two start: 0, 100
    asyncio.run(ingest_stream.ingest_oldest_first([a, b], result, budget))
    assert ingested == ["a1", "b1"]
    assert result.resume == "code-a:commits@a1+1", "a2 (−40 s) waited longer than b2 (−10 s)"
    assert ingest_budget.cut_points(result.resume) == {"commits": "code-a"}


def test_an_old_dated_backlog_ties_at_the_horizon_instead_of_holding_the_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    now = int(clock.time())
    times = {"old1": now - 90 * 86400, "old2": now - 89 * 86400, "new1": now - 60}
    ingested = _wire_streams(monkeypatch, clock, times)
    a, _ = _plan("code-a", ["old1", "old2"])
    b, _ = _plan("code-b", ["new1"])
    asyncio.run(
        ingest_stream.ingest_oldest_first(
            [a, b],
            ingest_budget.CodeIngestResult(),
            ingest_budget.PassBudget(clock.now, clock.now + 1000.0),
        )
    )
    assert ingested == ["old1", "old2", "new1"]  # both old ones are the horizon: entry order
    assert ingest_stream._OLDEST_FIRST_HORIZON_S == 86_400


def test_two_old_backlogs_share_the_horizon_round_robin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commits past the horizon all sort at the floor; `landed` breaks the tie
    so two entries with old backlogs alternate — entry order alone drained the
    first one before the second got a commit."""
    clock = _Clock()
    now = int(clock.time())
    old = now - 30 * 86400
    times = {s: old for s in ("a1", "a2", "a3", "b1", "b2", "b3")}
    ingested = _wire_streams(monkeypatch, clock, times)
    a, _ = _plan("code-a", ["a1", "a2", "a3"])
    b, _ = _plan("code-b", ["b1", "b2", "b3"])
    asyncio.run(
        ingest_stream.ingest_oldest_first(
            [a, b],
            ingest_budget.CodeIngestResult(),
            ingest_budget.PassBudget(clock.now, clock.now + 1000.0),
        )
    )
    assert ingested == ["a1", "b1", "a2", "b2", "a3", "b3"]


# ---------------------------------------------------------------------------
# a commit that cannot finish waits for the next pass
# ---------------------------------------------------------------------------


def test_a_commit_that_would_run_past_the_deadline_is_not_started_unless_it_is_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 2.5 s/file a 300-file commit is 750 s: started at 470 s it would run
    the pass into its 900 s timeout. The unit that must fit is one ROUND (25
    files), because a big commit lands in round slices under the pass clock (tests in
    test_code_ingest_large_commit_slices.py). So after s1 the big
    commit starts — as a slice — while one round still fits; a commit whose
    round cannot fit waits."""
    clock = _Clock()
    now = int(clock.time())
    monkeypatch.setattr(settings, "code_ingest_max_pass_seconds", 900)
    monkeypatch.setattr(settings, "code_ingest_seconds_per_file", 2.5)
    times = {"s1": now - 100, "big": now - 90, "s2": now - 80}
    files = {"s1": 180, "big": 300, "s2": 4}
    ingested = _wire_streams(monkeypatch, clock, times, files=files, cost=450.0)
    a, _ = _plan("code-a", ["s1"])
    b, _ = _plan("code-b", ["big"])
    c, _ = _plan("code-c", ["s2"])
    result = ingest_budget.CodeIngestResult()
    budget = ingest_budget.PassBudget(started=clock.now, stop_after=clock.now + 675.0)
    asyncio.run(ingest_stream.ingest_oldest_first([a, b, c], result, budget))
    # s1 (first) runs to 450 s; one round of big (25 × 2.5 s) fits → it starts
    # (the fake ingests it whole; the real ingest slices it under the clock)
    assert ingested[:2] == ["s1", "big"]
    # a commit whose single round cannot fit the time left waits
    assert (
        ingest_stream._Pace(budget).fits(300) is True
    )  # first unit of a fresh pace always may start
    late = ingest_stream._Pace(
        ingest_budget.PassBudget(started=clock.now - 850.0, stop_after=clock.now - 175.0)
    )
    late.observe(1, 2.5)
    assert late.fits(300) is False
    # next pass: big is the first commit and runs whatever its size
    ingested.clear()
    b2, _ = _plan("code-b", ["big"])
    asyncio.run(
        ingest_stream.ingest_oldest_first(
            [b2],
            ingest_budget.CodeIngestResult(),
            ingest_budget.PassBudget(clock.now, clock.now + 675.0),
        )
    )
    assert ingested == ["big"]


def test_the_pace_learns_the_seconds_per_file_it_observes() -> None:
    pace = ingest_stream._Pace(ingest_budget.PassBudget(0.0, 675.0))
    assert pace.fits(10_000), "nothing landed yet: the first commit always starts"
    pace.observe(100, 100.0)  # 1 s/file seen
    assert pace.landed == 1 and pace.rate == pytest.approx(
        0.7 * settings.code_ingest_seconds_per_file + 0.3
    )


# ---------------------------------------------------------------------------
# honest freshness
# ---------------------------------------------------------------------------


class _AsOfRepo:
    def __init__(self, fetched: datetime | None) -> None:
        self.fetched = fetched

    async def all_state(self, org: str, ns: str) -> dict[str, Any]:
        st: dict[str, Any] = {
            "git_head": {"value": "a" * 40, "updated_at": datetime(2026, 9, 24, 13, 23)}
        }
        if self.fetched is not None:
            st["clone_head"] = {"value": "a" * 40, "updated_at": self.fetched}
        return st


def _as_of(monkeypatch: pytest.MonkeyPatch, fetched: datetime | None) -> dict[str, Any]:
    cfg = SimpleNamespace(git_dir="loom.git", git_branch="main")
    monkeypatch.setattr("server.code.domain.index.repos.repo_for_namespace", lambda org, ns: cfg)

    async def lag(cfg: Any, head: str) -> int:
        return 0  # the mirror's own tip: "0 behind"

    monkeypatch.setattr(cc, "_lag_commits_cached", lag)
    cc.invalidate_index_as_of("o", "code-loom")
    try:
        return asyncio.run(cc.index_as_of(_AsOfRepo(fetched), "o", "code-loom"))  # type: ignore[arg-type]
    finally:
        cc.invalidate_index_as_of("o", "code-loom")


def test_a_mirror_that_has_not_fetched_for_an_hour_reads_stale_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    three_h = (datetime.now(UTC) - timedelta(hours=3, minutes=28)).replace(tzinfo=None)
    block = _as_of(monkeypatch, three_h)
    assert block["lag_commits"] == 0
    assert block["stale"] is True and block["status"] == "stale"
    assert block["stale_reason"] == "mirror_not_fetched"
    assert block["fetched_at"] == three_h.isoformat()


def test_a_recent_fetch_or_no_row_keeps_todays_block(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = (datetime.now(UTC) - timedelta(minutes=14)).replace(tzinfo=None)
    for fetched in (recent, None):
        block = _as_of(monkeypatch, fetched)
        assert block["status"] == "fresh" and block["stale"] is False
        assert "stale_reason" not in block and "fetched_at" not in block, (
            "no tokens on a healthy answer"
        )
    monkeypatch.setattr(settings, "code_fetch_stale_after_seconds", 0)
    assert _as_of(monkeypatch, datetime(2026, 1, 1))["status"] == "fresh", "0 disables"


def test_mirror_fetch_overdue_reads_naive_clickhouse_utc() -> None:
    now = datetime(2026, 9, 24, 16, 45, tzinfo=UTC)
    assert cc.mirror_fetch_overdue(datetime(2026, 9, 24, 13, 17), now) is True
    assert cc.mirror_fetch_overdue(datetime(2026, 9, 24, 16, 30), now) is False
    assert (
        cc.mirror_fetch_overdue(None, now) is False and cc.mirror_fetch_overdue("x", now) is False
    )


def test_run_repo_skips_the_fetch_its_mirror_did_and_leaves_the_commits_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry turn is cheap (no ingest in it), so every entry is
    reached every pass; the commits wait for the scheduler."""
    import server.code.app.index.registry as registry
    import server.code.store.index as code_repo_mod

    fetched: list[str] = []

    async def fetch(git_dir: str, remote: str, branch: str, token: str = "") -> None:
        fetched.append(branch)

    async def head(git_dir: str, branch: str) -> str:
        return "c3"

    async def between(git_dir: str, a: str, b: str, limit: int) -> list[str]:
        return ["c1", "c2", "c3"]

    async def none(*a: Any, **k: Any) -> None:
        return None

    async def zero(*a: Any, **k: Any) -> int:
        return 0

    class _Repo(_StateRepo):
        def __init__(self) -> None:
            super().__init__()
            self.state["git_head"] = "c0"

    async def boom(*a: Any, **k: Any) -> int:
        raise AssertionError("the entry turn must not ingest")

    monkeypatch.setattr("os.path.isdir", lambda d: True)
    monkeypatch.setattr(registry, "ensure_registered", none)
    monkeypatch.setattr(code_repo_mod, "CodeRepo", _Repo)
    monkeypatch.setattr(gitrepo, "fetch", fetch)
    monkeypatch.setattr(gitrepo, "head_sha", head)
    monkeypatch.setattr(gitrepo, "commits_between", between)
    monkeypatch.setattr(ingest_fetch, "fetch_extra_branches", zero)
    monkeypatch.setattr(ingest_fetch, "fetch_missing_merges", zero)
    monkeypatch.setattr(ingest_fetch, "fetch_pr_heads", zero)
    monkeypatch.setattr(ingest_fetch, "_publish_clone_head", none)
    monkeypatch.setattr(w, "_publish_clone_head", none)
    monkeypatch.setattr(ingest_commit, "_ingest_commit", boom)
    monkeypatch.setattr(ingest_stream, "_ingest_commit", boom)
    ingest_fetch._FETCHED_THIS_PASS.clear()
    ingest_fetch._FETCHED_THIS_PASS.add(("pub.git", "26.1"))
    try:
        shared = asyncio.run(
            w._run_repo(_entry("code-a-26-1", "pub.git", "26.1"), ingest_budget.CodeIngestResult())
        )
        solo = asyncio.run(
            w._run_repo(_entry("code-solo", "solo.git", "main"), ingest_budget.CodeIngestResult())
        )
    finally:
        ingest_fetch._FETCHED_THIS_PASS.clear()
    assert fetched == ["main"], "the prefetched branch is not fetched again"
    assert shared is not None and shared.pending == ["c1", "c2", "c3"] and shared.commits == 0
    assert solo is not None and solo.pending == ["c1", "c2", "c3"]
