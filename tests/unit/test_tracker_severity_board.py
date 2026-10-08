"""The bug board: one aggregating statement over
`github_issues FINAL` behind the privacy predicate, counted per component
and severity; the router runs it without a `limit`; the answer renders one
line per component."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from server.tracker.api.models import TrackerAnswerInput
from server.tracker.app import handlers as h
from server.tracker.app import router
from server.tracker.app import source as tsrc
from server.tracker.domain.errors import TrackerUnavailable
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG = "clickhouse"
PUB, PRIV = "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"

BOARD = [
    {
        "component": "MergeTree",
        "severity": "critical",
        "open": 1,
        "untested": 1,
        "flaky": 0,
        "bisected": 0,
    },
    {
        "component": "MergeTree",
        "severity": "medium",
        "open": 27,
        "untested": 20,
        "flaky": 0,
        "bisected": 4,
    },
    {
        "component": "MergeTree",
        "severity": "low",
        "open": 206,
        "untested": 179,
        "flaky": 1,
        "bisected": 4,
    },
    {"component": "Keeper", "severity": "", "open": 3, "untested": 3, "flaky": 0, "bisected": 0},
]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(f'{{"{REPO}": "{PUB}", "{PREPO}": "{PRIV}"}}', restricted=PRIV),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    monkeypatch.setattr(settings, "tracker_live_budget_ms", 300)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


class _Upstream:
    def __init__(self, rows: list[dict[str, Any]], delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any], str, float | None]] = []
        self.rows = rows
        self.delay = delay

    async def __call__(self, sql, params=None, *, credential="qa", max_execution_s=None, **kw):
        self.calls.append((" ".join(sql.split()), dict(params or {}), credential, max_execution_s))
        if self.delay:
            await asyncio.sleep(self.delay)
        return list(self.rows)


@pytest.mark.asyncio
async def test_a_slow_board_is_unavailable_within_the_point_read_budget(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_live_budget_ms", 10)  # floor: 50 ms
    monkeypatch.setattr(qi, "query_rows", _Upstream(BOARD, delay=0.3))
    with pytest.raises(TrackerUnavailable, match="severity_board exceeded 50 ms"):
        await src.TrackerSource(ORG, PUB).open_issue_counts_by_severity(ORG, PUB)


@pytest.mark.asyncio
async def test_statement_is_one_grouped_read_behind_the_privacy_wall(monkeypatch) -> None:
    up = _Upstream(BOARD)
    monkeypatch.setattr(qi, "query_rows", up)
    rows = await src.TrackerSource(ORG, PUB).open_issue_counts_by_severity(ORG, PUB)
    assert len(up.calls) == 1
    sql, params, cred, budget_s = up.calls[0]
    assert cred == "qa" and sql.count("is_private = 0 AND") == 1
    assert "FROM qa_intelligence.github_issues FINAL" in sql
    assert "closed_at <= toDateTime64(0, 3)" in sql  # open = never closed (no state column)
    assert "GROUP BY primary_component, severity_level" in sql and "HAVING count() > 0" in sql
    assert "ORDER BY primary_component ASC, multiIf(severity_level = 'critical', 0" in sql
    assert sql.rstrip().endswith("LIMIT 2000")
    assert "countIf(coalesce(has_test, 0) = 0) AS untested" in sql
    assert "countIf(is_flaky_test = 1) AS flaky" in sql
    assert "countIf(triage_bisect_outcome != '') AS bisected" in sql
    # the whole board: no component and no window predicate
    assert "primary_component IN" not in sql and "INTERVAL" not in sql
    assert params == {"repos": [REPO]}
    assert budget_s is None  # the point-read budget: the caller's deadline, no upstream cap
    assert rows[0] == BOARD[0] and rows[-1]["severity"] == "unrated" and rows[-1]["open"] == 3
    # the cache pays once
    await src.TrackerSource(ORG, PUB).open_issue_counts_by_severity(ORG, PUB)
    assert len(up.calls) == 1


@pytest.mark.asyncio
async def test_components_and_window_are_parameters(monkeypatch) -> None:
    up = _Upstream(BOARD[:3])
    monkeypatch.setattr(qi, "query_rows", up)
    await src.TrackerSource(ORG, PUB).open_issue_counts_by_severity(
        ORG, PUB, components=["MergeTree", "Keeper", "", "MergeTree"], days=90
    )
    sql, params, _, _ = up.calls[0]
    assert "primary_component IN {comps:Array(String)}" in sql
    assert "created_at >= now64(3) - INTERVAL {days:UInt32} DAY" in sql
    assert params == {"repos": [REPO], "comps": ["Keeper", "MergeTree"], "days": 90}
    # a different key, a different read
    await src.TrackerSource(ORG, PUB).open_issue_counts_by_severity(ORG, PUB, components=["Keeper"])
    assert len(up.calls) == 2 and up.calls[1][1] == {"repos": [REPO], "comps": ["Keeper"]}


@pytest.mark.asyncio
async def test_restricted_namespace_has_no_privacy_predicate(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)

    async def _restricted(org, ns):
        return ns == PRIV

    import server.identity.app.namespace_classification as ncls

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    rows = await src.TrackerSource(ORG, PRIV).open_issue_counts_by_severity(ORG, PRIV)
    assert rows == [] and "is_private" not in up.calls[0][0]
    assert up.calls[0][1] == {"repos": [PREPO]}


# ---- the router and the answer ---------------------------------------------------


class _Src:
    def __init__(self, rows: list[dict[str, Any]], *, restricted: bool = False) -> None:
        self.cfg = type("C", (), {"restricted": restricted})()
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def open_issue_counts_by_severity(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.calls.append(("open_issue_counts_by_severity", a, k))
        return list(self.rows)


@pytest.fixture
def wired(monkeypatch):
    holder: dict[str, Any] = {}
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: holder["src"])

    def _use(s: _Src) -> _Src:
        holder["src"] = s
        return s

    return _use


@pytest.mark.asyncio
async def test_router_runs_the_board_without_a_limit_and_renders_one_line_per_component(
    wired,
) -> None:
    s = wired(_Src(BOARD))
    ans = await router.answer(
        "what kinds of bugs are there in MergeTree", org=ORG, tracker_namespace=PUB
    )
    assert ans.intent == "severity_board" and len(ans.sections) == 1
    sec = ans.sections[0]
    assert sec.template == "severity_board" and sec.access == "public" and sec.status == "served"
    assert sec.rows == BOARD and sec.cites == "issues, counted per cell"
    # every cell, never the first ten: the reader gets no limit
    assert s.calls == [
        ("open_issue_counts_by_severity", (ORG, PUB), {"components": ["MergeTree"], "days": 0})
    ]
    assert ans.as_dict()["searched"] == ["severity_board(components=['MergeTree'])"]
    lines = router.render_lines(ans)
    assert lines[0] == (
        "Open issues by component and severity (2 components, 237 open; searched severity_board):"
    )
    assert (
        lines[1]
        == "- MergeTree: 1 critical · 27 medium · 206 low; 200 untested, 1 flaky, 8 bisected"
    )
    assert lines[2] == "- Keeper: 3 unrated; 3 untested"
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_whole_board_lines_are_capped_by_max_rows_and_empty_is_empty(wired) -> None:
    many = [
        {
            "component": f"C{i:02d}",
            "severity": "low",
            "open": 100 - i,
            "untested": 0,
            "flaky": 0,
            "bisected": 0,
        }
        for i in range(12)
    ]
    wired(_Src(many))
    ans = await router.answer("bug board", org=ORG, tracker_namespace=PUB)
    assert ans.sections[0].params == {"components": [], "days": 0}
    lines = router.render_lines(ans, max_rows=3)
    assert lines[0].startswith("Open issues by component and severity (12 components, ")
    assert [ln.split(":")[0] for ln in lines[1:]] == ["- C00", "- C01", "- C02"]
    wired(_Src([]))
    ans = await router.answer("open bugs by component and severity", org=ORG, tracker_namespace=PUB)
    assert ans.sections[0].status == "empty" and router.render_lines(ans) == []


@pytest.mark.asyncio
async def test_tracker_answer_carries_the_board_rows_and_lines(wired) -> None:
    wired(_Src(BOARD))
    out = await h.handle_tracker_answer(
        TrackerAnswerInput(
            org=ORG, namespace=PUB, question="severity breakdown of open issues in MergeTree"
        )
    )
    assert out.verdict == "answered" and out.intent == "severity_board"
    sec = out.sections[0]
    assert sec["template"] == "severity_board" and sec["rows"] == BOARD
    assert set(sec["rows"][0]) == {"component", "severity", "open", "untested", "flaky", "bisected"}
    assert out.lines[1].startswith("- MergeTree: 1 critical · 27 medium · 206 low; 200 untested")
    assert out.searched == ["severity_board(components=['MergeTree'])"]
    out = await h.handle_tracker_answer(
        TrackerAnswerInput(org=ORG, namespace=PUB, question="bug board", explain_only=True)
    )
    assert out.verdict == "planned" and out.sections[0]["status"] == "planned"
