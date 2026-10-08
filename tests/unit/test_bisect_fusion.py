"""Report-time bisect fuses every signal the report carries."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.code.app import bisect_fusion as cbf
from server.settings import settings
from server.tracker.domain import bisect as bi
from server.tracker.domain import bisect_fusion as fu

FIXTURE = Path(__file__).parent / "fixtures" / "bisect_fusion_cases.json"


def test_a_cued_suspect_is_read_and_a_denied_one_is_not() -> None:
    text = (
        "Regression after #104860 (join runtime filters).\n"
        "The bisect boundary was rejected — PR #97721 only modifies TCP handler code.\n"
        "See also #99000 for context.\n"
        "Finding: `h_pr95919_001`\n"
        "This issue is #112574."
    )
    cued, mentioned = fu.report_suspects(text, own_number=112574)
    assert cued == [104860, 95919]
    assert mentioned == [97721, 99000]


def test_identifiers_split_into_their_words() -> None:
    toks = fu.tokens("Flaky test: 04611_join_runtime_filters_index_analysis; initialQueryStartTime")
    assert {"join", "runtime", "filter", "index", "analysis", "initial", "start", "time"} <= set(
        toks
    )
    assert "analysi" not in toks  # a -sis word is not a plural


def test_a_version_that_is_no_release_is_dropped() -> None:
    assert fu.sane_version("10.6", bi.parse_version) == ""
    assert fu.sane_version("26.4", bi.parse_version) == "26.4"
    assert fu.sane_version("", bi.parse_version) == ""


def _row(n: int, title: str, files: list[str], closed: str, body: str = "") -> dict:
    return {
        "pr_number": n,
        "title": title,
        "closed_at": closed,
        "minor_version": "26.8",
        "affected_file_paths": files,
        "cl": "",
        "body": body,
    }


def test_fusion_ranks_the_pr_every_signal_agrees_on_first() -> None:
    rows = [
        _row(1, "Refactor logging", ["src/Common/Logger.cpp"], "2026-08-01 00:00:00"),
        _row(
            2,
            "Fix filter push-down over ANY INNER JOIN",
            ["src/Processors/QueryPlan/Optimizations/filterPushDown.cpp"],
            "2026-08-19 00:00:00",
        ),
        _row(
            3,
            "Speed up JOIN hash tables",
            ["src/Interpreters/HashJoin/HashJoin.cpp"],
            "2026-08-10 00:00:00",
        ),
    ]
    sig = fu.window_signals(
        rows,
        text="Lost primary-key filter pushdown across ANY INNER JOIN",
        paths=["src/Processors/QueryPlan/Optimizations/filterPushDown.cpp"],
        vector_order=[2, 3, 1],
        scan_order=[3, 2],
        before=datetime(2026, 9, 1),
    )
    assert sig["exact_paths"] == [2] and sig["lexical"][0] == 2 and sig["recency"][0] == 2
    order, why = fu.fuse(sig)
    assert order[0] == 2 and {"vector", "lexical", "exact_paths"} <= set(why[2])


def test_a_cued_suspect_leads_and_a_mention_is_evidence() -> None:
    sig = {"scan": [5, 6], "vector": [6, 5]}
    order, why = fu.fuse(sig, mentioned=[7], cued=[9])
    assert order[0] == 9 and why[9][0] == "named_by_report"
    assert 7 in order and "mentioned_by_report" in why[7]


def test_the_frozen_held_out_cases_fuse_their_culprit_first() -> None:
    cases = json.loads(FIXTURE.read_text())
    assert len(cases) == 3
    for c in cases:
        sig = fu.window_signals(
            c["rows"],
            text=c["text"],
            paths=c["paths"],
            vector_order=c["vector_order"],
            scan_order=[],
            before=datetime.fromisoformat(c["created"]),
        )
        order, _ = fu.fuse(sig)
        assert order[0] == c["culprit"], c["id"]


def _cand(n: int, **kw) -> bi.Candidate:
    return bi.Candidate(
        pr_number=n,
        title=f"pr {n}",
        author="",
        merged_at="2026-08-01T00:00:00",
        minor_version="26.8",
        **kw,
    )


def test_marks_after_fusion_only_demote() -> None:
    pool = [_cand(1), _cand(2), _cand(3)]
    marks = {1: SimpleNamespace(demoted=True), 2: SimpleNamespace(demoted=False)}
    assert [c.pr_number for c in fu.keep_fused_order(pool, marks)] == [2, 3, 1]


def test_suspects_lead_unless_a_known_fix_already_does() -> None:
    pool = [_cand(1), _cand(2), _cand(3)]
    assert [c.pr_number for c in fu.lead_suspects(pool, [3])] == [3, 1, 2]
    blamed = [_cand(1, blamed_by_fix={"sha": "x"}), _cand(2), _cand(3)]
    assert [c.pr_number for c in fu.lead_suspects(blamed, [3])] == [1, 2, 3]


def test_no_window_bound_or_no_source_means_no_prefetch() -> None:
    src = SimpleNamespace()
    assert (
        cbf.start_prefetch(src, "o", "t", versions=[], before=None, report_at=None, text="x")
        is None
    )
    assert (
        cbf.start_prefetch(
            None, "o", "t", versions=["26.8"], before=None, report_at=datetime(2026, 9, 1), text="x"
        )
        is None
    )


def test_a_time_window_reaches_back_the_configured_days(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_bisect_window_days", 90, raising=False)
    wall, floor = cbf._window_bounds([], None, datetime(2026, 9, 1))
    assert wall == datetime(2026, 9, 1) and floor == datetime(2026, 6, 3)
    assert cbf._window_bounds(["26.8"], None, datetime(2026, 9, 1)) == (datetime(2026, 9, 1), None)


@pytest.mark.asyncio
async def test_fusion_stage_adds_window_prs_the_scan_never_saw() -> None:
    rows = {
        1002: _row(
            1002, "Fix filter push-down over ANY INNER JOIN", ["src/a.cpp"], "2026-08-19 00:00:00"
        ),
        1003: _row(1003, "Unrelated", ["src/b.cpp"], "2026-08-10 00:00:00"),
    }

    async def done() -> tuple:
        return rows, [1002, 1003], ""

    task = asyncio.ensure_future(done())
    pool, summary, got = await cbf.fusion_stage(
        task,
        [_cand(1003)],
        report_at=datetime(2026, 9, 1),
        before=None,
        text="filter push-down over ANY INNER JOIN, regression after #1002",
        paths=["src/a.cpp"],
    )
    assert [c.pr_number for c in pool][:2] == [1002, 1003]
    assert pool[0].why[0] == "named_by_report" and pool[0].matched_paths == ["src/a.cpp"]
    assert summary["window_prs"] == 2 and summary["suspects"]["named"] == [1002] and got is rows


@pytest.mark.asyncio
async def test_a_failed_window_read_keeps_the_scan() -> None:
    async def failed() -> tuple:
        return {}, [], "window_unavailable"

    pool = [_cand(1)]
    got, summary, _ = await cbf.fusion_stage(
        asyncio.ensure_future(failed()),
        pool,
        report_at=None,
        before=None,
        text="",
        paths=[],
    )
    assert got == pool and summary == {"status": "window_unavailable"}


@pytest.mark.asyncio
async def test_rerank_is_off_by_default_and_keeps_order_on_failure(monkeypatch) -> None:
    pool = [_cand(1), _cand(2)]
    got, info = await cbf.rerank_stage(pool, text="report")
    assert got == pool and info == {}

    class Boom:
        async def complete(self, **_kw):
            raise RuntimeError("down")

    import server.llm.client as llm

    monkeypatch.setattr(llm, "get_llm_client_for_model", lambda _m: Boom())
    got, info = await cbf.rerank_stage(pool, text="report", force=True)
    assert got == pool and info == {"status": "failed", "trigger": "asked"}


@pytest.mark.asyncio
async def test_rerank_puts_the_models_pick_first(monkeypatch) -> None:
    class Pick:
        async def complete(self, **_kw):
            return SimpleNamespace(text='{"ranking":[{"i":2,"p":0.8,"why":"x"}]}')

    import server.llm.client as llm

    monkeypatch.setattr(llm, "get_llm_client_for_model", lambda _m: Pick())
    got, info = await cbf.rerank_stage([_cand(1), _cand(2)], text="report", force=True)
    assert [c.pr_number for c in got] == [2, 1] and got[0].why[0] == "llm_rerank"
    assert info["reranked"] == 1
