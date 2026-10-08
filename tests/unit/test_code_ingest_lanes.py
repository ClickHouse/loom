"""Commit-phase lanes: `code_ingest_lanes` entries ingest side by side from
the one oldest-first order, and no entry ever has two commits in flight, so
each entry's commits still land oldest first and its watermark only moves
forward. One lane is the sequential phase."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import pass_budget as ingest_budget
from server.code.app.index import pass_commit as ingest_commit_mod
from server.code.app.index import pass_stream as ingest_stream
from server.settings import settings
from tests.unit.test_code_ingest_fair_fetch import _plan, _wire_streams
from tests.unit.test_code_ingest_pass_budget import _Clock


def _concurrent_ingest(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"order": [], "in_flight": {}, "max": 0, "same_entry_overlap": False}

    async def ingest_commit(
        repo: Any, org: str, ns: str, git_dir: str, base: str, sha: str, **kw: Any
    ) -> None:
        flight = seen["in_flight"]
        if flight.get(ns):
            seen["same_entry_overlap"] = True
        flight[ns] = True
        seen["max"] = max(seen["max"], sum(flight.values()))
        await asyncio.sleep(0)  # yield: another lane may start here
        await asyncio.sleep(0)
        seen["order"].append(sha)
        flight[ns] = False

    monkeypatch.setattr(ingest_commit_mod, "_ingest_commit", ingest_commit)
    monkeypatch.setattr(ingest_stream, "_ingest_commit", ingest_commit)
    return seen


@pytest.mark.parametrize("lanes", [2, 3])
def test_lanes_run_entries_side_by_side_and_never_one_entry_twice(
    monkeypatch: pytest.MonkeyPatch, lanes: int
) -> None:
    clock = _Clock()
    now = int(clock.time())
    times = {
        "a1": now - 50,
        "a2": now - 40,
        "a3": now - 30,
        "b1": now - 45,
        "b2": now - 35,
        "c1": now - 20,
    }
    _wire_streams(monkeypatch, clock, times)
    seen = _concurrent_ingest(monkeypatch)
    monkeypatch.setattr(settings, "code_ingest_lanes", lanes)
    a, ra = _plan("code-a", ["a1", "a2", "a3"])
    b, rb = _plan("code-b", ["b1", "b2"])
    c, rc = _plan("code-c", ["c1"])
    result = ingest_budget.CodeIngestResult()
    asyncio.run(
        ingest_stream.ingest_oldest_first(
            [a, b, c], result, ingest_budget.PassBudget(clock.now, clock.now + 1000.0)
        )
    )
    assert seen["max"] == lanes, "lanes ran at once"
    assert not seen["same_entry_overlap"], "one entry never has two commits in flight"
    for prefix in "abc":
        mine = [s for s in seen["order"] if s.startswith(prefix)]
        assert mine == sorted(mine), f"code-{prefix} lands oldest first"
    assert (ra.state["git_head"], rb.state["git_head"], rc.state["git_head"]) == ("a3", "b2", "c1")
    assert result.commits_done == 6 and not result.partial


def test_one_lane_is_the_sequential_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    now = int(clock.time())
    times = {"a1": now - 50, "b1": now - 45, "a2": now - 40}
    _wire_streams(monkeypatch, clock, times)
    seen = _concurrent_ingest(monkeypatch)
    monkeypatch.setattr(settings, "code_ingest_lanes", 1)
    a, _ = _plan("code-a", ["a1", "a2"])
    b, _ = _plan("code-b", ["b1"])
    asyncio.run(
        ingest_stream.ingest_oldest_first(
            [a, b],
            ingest_budget.CodeIngestResult(),
            ingest_budget.PassBudget(clock.now, clock.now + 1000.0),
        )
    )
    assert seen["max"] == 1
    assert seen["order"] == ["a1", "b1", "a2"], "oldest first across entries"


@pytest.mark.parametrize("lanes", [1, 2])
def test_one_entrys_failed_setup_leaves_the_other_entries_their_commits(
    monkeypatch: pytest.MonkeyPatch, lanes: int
) -> None:
    """A stream's setup (or a cursor write) raising outside the commit's own
    isolation drops that entry for the pass, notes it on the pass, and the
    other entries still land; no lane is left running."""
    from server.workers import pass_context

    clock = _Clock()
    now = int(clock.time())
    _wire_streams(monkeypatch, clock, {"a1": now - 50, "b1": now - 45, "b2": now - 40})
    _concurrent_ingest(monkeypatch)
    monkeypatch.setattr(settings, "code_ingest_lanes", lanes)
    real_prepare = ingest_stream.CommitStream._prepare

    async def prepare(self: Any) -> dict[str, Any]:
        if self.cfg.namespace == "code-a":
            raise RuntimeError("embedder down")
        return await real_prepare(self)

    monkeypatch.setattr(ingest_stream.CommitStream, "_prepare", prepare)
    a, ra = _plan("code-a", ["a1"])
    b, rb = _plan("code-b", ["b1", "b2"])
    result = ingest_budget.CodeIngestResult()
    ctx = pass_context.begin("code_ingest")

    async def run() -> set[asyncio.Task[Any]]:
        await ingest_stream.ingest_oldest_first(
            [a, b], result, ingest_budget.PassBudget(clock.now, clock.now + 1000.0)
        )
        return {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    try:
        left = asyncio.run(run())
    finally:
        pass_context.clear()
    assert left == set()
    assert rb.state["git_head"] == "b2" and "git_head" not in ra.state
    assert any("code-a" in e and "embedder down" in e for e in result.errors)
    assert ctx.count == 1 and ctx.failures[0][:2] == ("code-a", "RuntimeError")
