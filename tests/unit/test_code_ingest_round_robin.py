"""The commit phase's share across entries: one commit per entry per round, so an
entry with a few new commits is never starved by an entry with a large backlog.

Two backlogged entries (hundreds of commits) each get one commit per round, so a
one-commit entry lands in the first round however long the backlogs are."""

from __future__ import annotations

import asyncio

import pytest

from server.code.app.index import pass_budget as ingest_budget
from server.code.app.index import pass_stream as ingest_stream
from tests.unit.test_code_ingest_fair_fetch import _plan, _wire_streams
from tests.unit.test_code_ingest_pass_budget import _Clock


def _backlogs() -> tuple[list[str], list[str], list[str]]:
    """Two entries with backlogs of 400 and 300 commits, one with a single commit."""
    return (
        [f"a{i:03d}" for i in range(400)],
        [f"b{i:03d}" for i in range(300)],
        ["c1"],
    )


def _run(
    plans: list[ingest_budget.RepoPass], clock: _Clock, seconds: float
) -> ingest_budget.CodeIngestResult:
    result = ingest_budget.CodeIngestResult()
    budget = ingest_budget.PassBudget(started=clock.now, stop_after=clock.now + seconds)
    asyncio.run(ingest_stream.ingest_round_robin(plans, result, budget))
    return result


def test_a_one_commit_entry_lands_in_the_pass_a_backlog_would_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget fits 6 of the 701 pending commits. The backlogs each get a
    share; the one-commit entry lands in the first round."""
    clock = _Clock()
    big, other, small = _backlogs()
    ingested = _wire_streams(monkeypatch, clock)  # 10 s a commit
    a, ra = _plan("code-a", big)
    b, rb = _plan("code-b", other)
    c, rc = _plan("code-c", small)
    result = _run([a, b, c], clock, 55.0)  # starts at 0, 10, ..., 50
    assert ingested == ["a000", "b000", "c1", "a001", "b001", "a002"]
    assert rc.state["git_head"] == "c1", "the one-commit entry is served"
    assert (a.commits, b.commits, c.commits) == (3, 2, 1)
    assert ra.state["git_head"] == "a002" and rb.state["git_head"] == "b001"
    assert result.partial and result.commits_planned == 701 and result.commits_done == 6


def test_every_entry_lands_a_commit_each_pass_while_the_backlogs_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thirteen passes in a row: each one lands a commit of every entry still
    behind, and the shares stay within one commit of each other."""
    clock = _Clock()
    big, other, _ = _backlogs()
    small = [f"c{i}" for i in range(20)]
    ingested = _wire_streams(monkeypatch, clock)
    heads = {"code-a": "w0", "code-b": "w0", "code-c": "w0"}
    left = {"code-a": big, "code-b": other, "code-c": small}
    for _ in range(13):
        ingested.clear()
        plans = []
        for ns, pending in left.items():
            if not pending:
                continue
            plan, repo = _plan(ns, pending)
            plan.watermark = heads[ns]
            plans.append((plan, repo))
        _run([p for p, _ in plans], clock, 55.0)
        landed = {p.cfg.namespace: p.commits for p, _ in plans}
        assert all(n >= 1 for n in landed.values()), landed
        assert max(landed.values()) - min(landed.values()) <= 1, landed
        for plan, repo in plans:
            ns = plan.cfg.namespace
            heads[ns] = repo.state["git_head"]
            left[ns] = left[ns][plan.commits :]
    # six commits a pass, two each: the twenty-commit entry caught up in ten passes
    assert left["code-c"] == [] and len(left["code-a"]) == 400 - 20 - 3 * 3


def test_a_round_the_budget_ends_resumes_next_pass_at_the_entry_it_did_not_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three entries, one big commit each, a budget for two: the first cut
    names the entry the round did not reach, and the next pass's entry order
    (`rotate_to` on that cut) serves it first."""
    clock = _Clock()
    ingested = _wire_streams(monkeypatch, clock, cost=100.0)
    a, _ = _plan("code-a", ["a1", "a2"])
    b, _ = _plan("code-b", ["b1", "b2"])
    c, rc = _plan("code-c", ["c1"])
    result = _run([a, b, c], clock, 150.0)  # starts at 0 and 100
    assert ingested == ["a1", "b1"]
    starts = ingest_budget.cut_points(result.resume)
    assert starts == {"commits": "o/code-c"}, result.resume

    ingested.clear()
    a2, _ = _plan("code-a", ["a2"])
    b2, _ = _plan("code-b", ["b2"])
    c2, rc = _plan("code-c", ["c1"])
    order = ingest_budget.rotate_to(
        [a2, b2, c2], starts["commits"], key=lambda p: ingest_budget.cut_key(p.cfg)
    )
    _run(order, clock, 150.0)
    assert ingested == ["c1", "a2"] and rc.state["git_head"] == "c1"


@pytest.mark.parametrize("lanes", [1, 2])
def test_each_entry_still_lands_oldest_first(monkeypatch: pytest.MonkeyPatch, lanes: int) -> None:
    """Round-robin across entries; within one, the watermark only moves forward."""
    from server.settings import settings

    clock = _Clock()
    big, other, small = _backlogs()
    ingested = _wire_streams(monkeypatch, clock)
    monkeypatch.setattr(settings, "code_ingest_lanes", lanes)
    a, _ = _plan("code-a", big[:5])
    b, _ = _plan("code-b", other[:3])
    c, _ = _plan("code-c", small)
    result = _run([a, b, c], clock, 10_000.0)
    for prefix in "abc":
        mine = [s for s in ingested if s.startswith(prefix)]
        assert mine == sorted(mine), f"code-{prefix} lands oldest first"
    assert result.commits_done == 9 and not result.partial
