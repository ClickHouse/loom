"""An ingest pass serves the entry it starved first.

Each phase of the pass (the commits, the bootstrap slices, the walk) starts
at the entry the previous pass cut it at, read from the cursor that pass
left; the commit phase stops starting commits at its share of the budget so
the walk — the reconcile heal and the extractor re-chunk — runs every pass.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import ingest_pass as w
from server.code.app.index import pass_budget as ingest_budget
from server.settings import settings
from server.workers import pass_context
from tests.unit.test_code_ingest_pass_budget import _cfg, _Clock, _wire_run

# ---------------------------------------------------------------------------
# the cursor's shape: one cut per phase
# ---------------------------------------------------------------------------


def test_a_cut_per_phase_is_kept_and_the_cursor_lists_them_first_cut_first() -> None:
    r = ingest_budget.CodeIngestResult()
    r.cut("code-p:commits@abcdef123456+3", cfg=_cfg("code-p"))
    r.cut("code-q:commits", cfg=_cfg("code-q"))  # a later cut of the same phase: not the start
    r.cut("code-r:rechunk@700/22136", cfg=_cfg("code-r"))
    r.cut("code-s:heal@10/40", cfg=_cfg("code-s"))
    assert r.partial is True
    assert r.cuts == {
        "commits": "code-p:commits@abcdef123456+3",
        "walk": "code-r:rechunk@700/22136",
    }
    assert r.resume == "code-p:commits@abcdef123456+3; code-r:rechunk@700/22136"
    assert ingest_budget.cut_points(r.resume) == {"commits": "code-p", "walk": "code-r"}


def test_every_leg_the_pass_writes_maps_to_its_phase() -> None:
    assert ingest_budget.cut_phase("code-a:commits") == "commits"
    assert ingest_budget.cut_phase("code-a:commits@abc+3") == "commits"
    assert ingest_budget.cut_phase("code-a:bootstrap@10/40") == "bootstrap"
    assert ingest_budget.cut_phase("code-a:walk") == "walk"
    assert ingest_budget.cut_phase("code-a:rechunk") == "walk"
    assert ingest_budget.cut_phase("code-a:rechunk@700/22136") == "walk"
    assert ingest_budget.cut_phase("code-a:heal@10/40") == "walk"
    assert (
        ingest_budget.cut_phase("") == ""
        and ingest_budget.cut_phase("garbage") == ""
        and ingest_budget.cut_phase("a:novel") == ""
    )
    assert ingest_budget.cut_points("") == {} and ingest_budget.cut_points("not a cursor") == {}


def test_rotation_starts_at_the_named_entry_and_is_the_identity_otherwise() -> None:
    a, b, c = _cfg("code-a"), _cfg("code-b"), _cfg("code-c")
    assert [e.namespace for e in ingest_budget.rotate_to([a, b, c], "code-b")] == [
        "code-b",
        "code-c",
        "code-a",
    ]
    assert [e.namespace for e in ingest_budget.rotate_to([a, b, c], "code-a")] == [
        "code-a",
        "code-b",
        "code-c",
    ]
    assert [e.namespace for e in ingest_budget.rotate_to([a, b, c], "")] == [
        "code-a",
        "code-b",
        "code-c",
    ]
    assert [e.namespace for e in ingest_budget.rotate_to([a, b, c], "code-gone")] == [
        "code-a",
        "code-b",
        "code-c",
    ]
    assert ingest_budget.rotate_to([], "code-a") == []


# ---------------------------------------------------------------------------
# the pass: the commit phase's share, the walk runs, the next pass rotates
# ---------------------------------------------------------------------------


def _three(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, cost: float
) -> tuple[list[str], list[Any]]:
    monkeypatch.setattr(settings, "code_ingest_max_pass_seconds", 1000)
    monkeypatch.setattr(settings, "code_ingest_pass_budget_fraction", 0.5)  # 500 s
    monkeypatch.setattr(settings, "code_ingest_walk_budget_share", 0.3)  # commits stop at 350 s
    cfgs = [_cfg("code-a"), _cfg("code-b"), _cfg("code-c")]
    plans = {
        c.namespace: ingest_budget.RepoPass(c, object(), "h", "w", False, walk=True) for c in cfgs
    }
    return _wire_run(monkeypatch, cfgs, plans, clock, cost=cost), cfgs


def _run_with_cursor(cursor: str) -> ingest_budget.CodeIngestResult:
    ctx = pass_context.begin("code_ingest", resume_cursor=cursor)
    try:
        result = asyncio.run(w.run_pass())
    finally:
        pass_context.clear()
    assert ctx.cursor == result.resume
    return result


def test_the_commit_phase_stops_at_its_share_so_the_walk_runs_in_the_same_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every leg costs 150 s of a 500 s budget whose commit phase stops
    starting entries at 350 s: the commits of a (0) and b (150) and c (300)
    start; the walk of a starts at 450 < 500, the walk of b would start at
    600. The walk's start is checked against the same 500 s the commits ran
    under; the commit share matters under load (next test)."""
    clock = _Clock()
    order, _ = _three(monkeypatch, clock, cost=150.0)
    result = _run_with_cursor("")
    assert order[:4] == ["commits:code-a", "commits:code-b", "commits:code-c", "walk:code-a"]
    assert result.partial is True and result.resume == "code-b:walk"


def test_under_commit_load_the_walk_still_gets_its_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each entry's commits cost 200 s: a (0) and b (200) start, c would
    start at 400 > 350 — the commit phase is cut at c and 100 s of the
    budget are left for the walk, which starts a's (400 < 500). Without the
    share, the commits of c would run too (400 < 500) and no walk would start
    in any pass while commits keep arriving."""
    clock = _Clock()
    order, _ = _three(monkeypatch, clock, cost=200.0)
    result = _run_with_cursor("")
    assert order == [
        "commits:code-a",
        "commits:code-b",
        "walk:code-a",
        "post:code-a",
        "post:code-b",
    ]
    assert result.resume == "code-c:commits; code-b:walk"
    assert w.pass_outcome(result)["resume"] == "code-c:commits; code-b:walk"


def test_the_next_pass_starts_each_phase_at_the_entry_the_last_one_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cursor the previous pass left says the commits stopped before c
    and the walk before b: this pass fetches c first (then a, b) and walks b
    first — the entries the last pass starved are served before the ones
    it already served."""
    clock = _Clock()
    order, _ = _three(monkeypatch, clock, cost=100.0)
    result = _run_with_cursor("code-c:commits; code-b:walk")
    assert order == [
        "commits:code-c",
        "commits:code-a",
        "commits:code-b",
        "walk:code-b",
        "walk:code-c",
        "post:code-c",
        "post:code-a",
        "post:code-b",
    ], order
    assert result.resume == "code-a:walk"


def test_an_entry_the_commit_phase_did_not_reach_owes_its_walk_to_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The walk runs over the entries this pass fetched; one the commit
    phase never reached has no head to walk at. The cursor still names it
    for the commits, so the next pass fetches it first and walks it."""
    clock = _Clock()
    order, _ = _three(monkeypatch, clock, cost=200.0)
    result = _run_with_cursor("code-c:commits; code-b:walk")
    assert order == [
        "commits:code-c",
        "commits:code-a",
        "walk:code-c",
        "post:code-c",
        "post:code-a",
    ], order
    assert result.resume == "code-b:commits; code-a:walk"


def test_a_single_namespace_trigger_is_not_rotated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`code.refresh` addresses one entry; the cursor's start does not apply."""
    clock = _Clock()
    order, cfgs = _three(monkeypatch, clock, cost=1.0)
    monkeypatch.setattr(w, "selected_repos", lambda org="", namespace="": [cfgs[1]])
    ctx = pass_context.begin("code_ingest", resume_cursor="code-c:commits")
    try:
        asyncio.run(w.run_pass(org="o", namespace="code-b"))
    finally:
        pass_context.clear()
    assert order == ["commits:code-b", "walk:code-b", "post:code-b"]
    assert ctx is not None


def test_the_share_is_a_setting_with_zero_meaning_one_budget() -> None:
    assert settings.code_ingest_walk_budget_share == 0.3
    b = ingest_budget.PassBudget(started=10.0, stop_after=110.0)
    assert b.phase(0.0) is b
    assert b.phase(0.3).stop_after == 80.0 and b.phase(0.3).started == 10.0
    assert b.phase(1.5).seconds == 0.0  # clamped: the phase starts nothing
