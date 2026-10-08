"""A weak fused top gets a second reading — the rerank, with
the evidence each candidate shares with the report, and the wider window's
candidates held behind the first window unless the model is confident."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from server.code.app import bisect_fusion as cbf
from server.settings import settings
from server.tracker.domain import bisect as bi
from server.tracker.domain import bisect_fusion as fu


def _cand(n: int, **kw) -> bi.Candidate:
    kw.setdefault("title", f"pr {n}")
    return bi.Candidate(
        pr_number=n, author="", merged_at="2026-08-01T00:00:00", minor_version="26.8", **kw
    )


def _row(n: int, title: str, files: list[str], body: str = "") -> dict:
    return {
        "pr_number": n,
        "title": title,
        "closed_at": "2026-08-01 00:00:00",
        "minor_version": "26.8",
        "affected_file_paths": files,
        "cl": "",
        "body": body,
    }


def test_a_close_top_is_weak_unless_the_report_names_it() -> None:
    assert fu.is_weak(margin=0.05, top_cued=False)
    assert not fu.is_weak(margin=0.3, top_cued=False)
    # a PR the report itself names never needs a second reading
    assert not fu.is_weak(margin=0.0, top_cued=True)


def test_the_margin_is_the_top_scores_relative_gap() -> None:
    assert fu.top_margin({"scan": [1]}) == 1.0
    assert fu.top_margin({"scan": [1, 2], "vector": [2, 1]}) == 0.0
    assert 0.0 < fu.top_margin({"scan": [1, 2], "vector": [1, 2]}) < 0.1
    assert fu.top_margin({"exact_paths": [1], "vector": [1, 2], "scan": [1, 2]}) > 0.3


def test_identifiers_are_the_names_a_report_uses_and_rare_ones_single_a_pr_out() -> None:
    text = (
        "`AsynchronousBoundedReadBuffer` with optimize_move_to_prewhere breaks "
        "toStartOfInterval(x) on the MergeTree table"
    )
    got = fu.report_identifiers(text)
    assert {
        "AsynchronousBoundedReadBuffer",
        "optimize_move_to_prewhere",
        "toStartOfInterval",
    } <= got
    texts = {i: "MergeTree optimize_move_to_prewhere" for i in range(40)}
    texts[99] = "AsynchronousBoundedReadBuffer prefetch"
    rare = fu.rare_identifiers(got | {"MergeTree"}, texts)
    assert "AsynchronousBoundedReadBuffer" in rare and "optimize_move_to_prewhere" not in rare


def test_an_outside_candidate_leads_only_on_confidence_and_evidence() -> None:
    ranked = [(9, 0.9), (8, 0.8), (1, 0.4)]
    order = fu.merge_outside(ranked, outside={8, 9}, evidence={8}, in_window=[1, 2, 3])
    # 9 is out of the window with no evidence: held behind every in-window PR
    assert order == [8, 1, 2, 3, 9]
    low = fu.merge_outside([(8, 0.3)], outside={8}, evidence={8}, in_window=[1, 2])
    assert low == [1, 2, 8]


def _weak_summary(pool: list[bi.Candidate], signals: dict) -> dict:
    return cbf._weakness(pool, cued=[], signals=signals, mentioned=[])


def test_fusion_reports_whether_its_top_is_weak() -> None:
    strong = _weak_summary(
        [_cand(1, matched_paths=["src/a.cpp"]), _cand(2)],
        {"exact_paths": [1], "vector": [1, 2], "scan": [1, 2]},
    )
    assert strong["weak"] is False and strong["margin"] > 0.1
    weak = _weak_summary([_cand(1), _cand(2)], {"vector": [1, 2], "lexical": [2, 1]})
    assert weak["weak"] is True


class _Pick:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.user = ""

    async def complete(self, **kw):
        self.user = kw["user"]
        return SimpleNamespace(text=json.dumps(self.reply))


@pytest.mark.asyncio
async def test_a_weak_top_is_reranked_without_being_asked(monkeypatch) -> None:
    import server.llm.client as llm

    pick = _Pick({"ranking": [{"i": 2, "p": 0.8, "why": "x"}]})
    monkeypatch.setattr(llm, "get_llm_client_for_model", lambda _m: pick)
    pool = [_cand(1), _cand(2)]
    got, info = await cbf.rerank_stage(pool, text="report", weak=False)
    assert got == pool and info == {}
    got, info = await cbf.rerank_stage(pool, text="report", weak=True)
    assert [c.pr_number for c in got] == [2, 1]
    assert info["trigger"] == "weak" and info["top_p"] == 0.8


@pytest.mark.asyncio
async def test_the_prompt_shows_the_evidence_a_candidate_shares(monkeypatch) -> None:
    import server.llm.client as llm

    pick = _Pick({"ranking": []})
    monkeypatch.setattr(llm, "get_llm_client_for_model", lambda _m: pick)
    rows = {
        1: _row(
            1, "Restore prefetch for small files", ["src/IO/AsynchronousBoundedReadBuffer.cpp"]
        ),
        2: _row(2, "Unrelated", ["src/b.cpp"]),
    }
    extra = _cand(3, title="Old change")
    await cbf.rerank_stage(
        [_cand(1, matched_paths=["src/IO/AsynchronousBoundedReadBuffer.cpp"]), _cand(2)],
        text="crash in `AsynchronousBoundedReadBuffer` after upgrade",
        rows_by_number=rows,
        weak=True,
        extras=[extra],
        cued=[2],
    )
    lines = pick.user.split("\n")
    first = next(i for i, x in enumerate(lines) if x.startswith("[1] #1 "))
    assert "AsynchronousBoundedReadBuffer" in " ".join(lines[first : first + 4])
    assert "named by the report" in pick.user and "merged BEFORE the release range" in pick.user


@pytest.mark.asyncio
async def test_an_unconfident_outside_pick_stays_behind_the_window(monkeypatch) -> None:
    import server.llm.client as llm

    pick = _Pick({"ranking": [{"i": 3, "p": 0.9, "why": "x"}, {"i": 2, "p": 0.6, "why": "y"}]})
    monkeypatch.setattr(llm, "get_llm_client_for_model", lambda _m: pick)
    got, info = await cbf.rerank_stage(
        [_cand(1), _cand(2)],
        text="report",
        weak=True,
        extras=[_cand(3)],
    )
    # #3 is outside the window and shares nothing with the report
    assert [c.pr_number for c in got] == [2, 1, 3] and info["wider_window"] == 1


@pytest.mark.asyncio
async def test_a_failed_rerank_keeps_the_fused_order(monkeypatch) -> None:
    class Boom:
        async def complete(self, **_kw):
            raise RuntimeError("down")

    import server.llm.client as llm

    monkeypatch.setattr(llm, "get_llm_client_for_model", lambda _m: Boom())
    pool = [_cand(1), _cand(2)]
    got, info = await cbf.rerank_stage(pool, text="report", weak=True, extras=[_cand(3)])
    assert [c.pr_number for c in got] == [1, 2, 3]
    assert info == {"status": "failed", "trigger": "weak"}


class _Source:
    def __init__(self, rows: list[dict], fail: bool = False) -> None:
        self.rows, self.fail, self.calls = rows, fail, []

    async def window_prs(self, org, ns, versions, *, before=None, after=None):
        self.calls.append((versions, before, after))
        if self.fail:
            raise RuntimeError("down")
        return self.rows


@pytest.mark.asyncio
async def test_the_wider_window_adds_only_what_the_first_one_lacked(monkeypatch) -> None:
    async def no_vec(_t: str) -> list[float]:
        return []

    monkeypatch.setattr(cbf, "_embed", no_vec)
    src = _Source(
        [
            _row(1, "In the first window", ["src/a.cpp"]),
            _row(7, "Grant filters for sources by URI pattern", ["src/Access/x.cpp"]),
        ]
    )
    got, rows = await cbf.wide_extras(
        src,
        "o",
        "t",
        good="",
        bad="26.3",
        master=False,
        versions=["26.1", "26.2", "26.3"],
        report_at=datetime(2026, 5, 1),
        in_window={1},
        text="grants on URI pattern",
        paths=[],
    )
    assert [c.pr_number for c in got] == [7] and got[0].why[0] == "wider_window" and 1 not in rows
    assert len(src.calls[0][0]) > 3  # twelve releases, not the first window's
    got, _ = await cbf.wide_extras(
        _Source([], fail=True),
        "o",
        "t",
        good="",
        bad="26.3",
        master=False,
        versions=["26.3"],
        report_at=None,
        in_window=set(),
        text="x",
        paths=[],
    )
    assert got == []


@pytest.mark.asyncio
async def test_a_time_window_report_reaches_back_the_wide_days(monkeypatch) -> None:
    async def no_vec(_t: str) -> list[float]:
        return []

    monkeypatch.setattr(cbf, "_embed", no_vec)
    monkeypatch.setattr(settings, "code_bisect_wide_days", 180, raising=False)
    src = _Source([])
    await cbf.wide_extras(
        src,
        "o",
        "t",
        good="",
        bad="",
        master=False,
        versions=[],
        report_at=datetime(2026, 7, 1),
        in_window=set(),
        text="x",
        paths=[],
    )
    versions, before, after = src.calls[0]
    assert versions == [] and before == datetime(2026, 7, 1) and (before - after).days == 180
