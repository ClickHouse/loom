"""A cell of the bug board lists every issue it counts. The
component's open-issue read takes the cell's severities — the predicate is
present only when given, the ceiling lifts from 50 to the cell limit — the
catalogue plans it from the question or the dashboard's anchors, the
section's title names the cell, and the restricted class renders as rows."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from server.tracker.app import router
from server.tracker.app import source as tsrc
from server.tracker.domain import catalogue as cat
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG = "clickhouse"
PUB, PRIV = "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"


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


ROW = {
    "source_id": f"{REPO}#119601",
    "url": "u",
    "title": "Zero-length columns_substreams.txt",
    "severity_level": "medium",
    "primary_component": "MergeTree",
    "created_at": None,
    "closed_at": None,
    "labels": ["bug"],
    "assignees": [],
}


# ---- the reader ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_severity_predicate_is_present_only_when_given(monkeypatch) -> None:
    up = _Upstream([ROW])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    rows = await s.open_issues_by_component(ORG, PUB, "MergeTree", limit=50)
    sql, params, _, budget = up.calls[-1]
    assert "severity_level IN" not in sql and "sev" not in params
    assert params["limit"] == 50 and rows[0]["number"] == 119601
    # the plain read keeps its 50-row ceiling whatever the caller asks
    await s.open_issues_by_component(ORG, PUB, "Keeper", limit=300)
    assert up.calls[-1][1]["limit"] == 50
    # with severities the predicate is a parameter, the ceiling is the cell's
    await s.open_issues_by_component(ORG, PUB, "MergeTree", limit=300, severities=["low"])
    sql, params, _, budget = up.calls[-1]
    assert "AND severity_level IN {sev:Array(String)}" in sql
    assert params["sev"] == ["low"] and params["limit"] == 300 == src.CELL_LIMIT
    assert "is_private = 0 AND primary_component = {component:String}" in sql
    assert (
        "ORDER BY multiIf(severity_level = 'critical', 0" in sql and "LIMIT {limit:UInt32}" in sql
    )
    # a point read: the default budget (no per-statement ceiling, the
    # caller's 300 ms deadline), not the text budget's server ceiling
    assert budget is None


@pytest.mark.asyncio
async def test_severity_names_are_normalised_in_python_not_sql(monkeypatch) -> None:
    up = _Upstream([ROW])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    await s.open_issues_by_component(
        ORG, PUB, "MergeTree", limit=300, severities=[" Low", "MEDIUM", "low", "bogus", "Unrated"]
    )
    sql, params, _, _ = up.calls[-1]
    # lower-cased, trimmed, unknown names dropped, once each, in rank order;
    # `unrated` is the empty stored level; no lower()/trim() around the column
    assert params["sev"] == ["medium", "low", ""]
    assert "lower(severity_level)" not in sql and "lowerUTF8" not in sql
    # nothing recognisable = no predicate, the plain 50-row read
    await s.open_issues_by_component(ORG, PUB, "MergeTree", limit=300, severities=["bogus"])
    sql, params, _, _ = up.calls[-1]
    assert "severity_level IN" not in sql and params["limit"] == 50
    assert src.normalise_severities(None) == [] and src.normalise_severities(["Critical"]) == [
        "critical"
    ]


@pytest.mark.asyncio
async def test_cells_are_cached_apart_from_the_plain_read(monkeypatch) -> None:
    up = _Upstream([ROW])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    await s.open_issues_by_component(ORG, PUB, "MergeTree", limit=50)
    await s.open_issues_by_component(ORG, PUB, "MergeTree", limit=300, severities=["medium"])
    await s.open_issues_by_component(ORG, PUB, "MergeTree", limit=300, severities=["low"])
    await s.open_issues_by_component(ORG, PUB, "MergeTree", limit=300, severities=["Medium"])
    assert len(up.calls) == 3  # the fourth is the second cell again, paid once


@pytest.mark.asyncio
async def test_a_restricted_namespace_reads_its_cell_without_the_privacy_wall(monkeypatch) -> None:
    import server.identity.app.namespace_classification as ncls

    async def _restricted(org, ns):
        return ns == PRIV

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    up = _Upstream([ROW])
    monkeypatch.setattr(qi, "query_rows", up)
    await src.TrackerSource(ORG, PRIV).open_issues_by_component(
        ORG, PRIV, "MergeTree", limit=300, severities=["medium"]
    )
    sql, params, _, _ = up.calls[-1]
    assert (
        "is_private = 0" not in sql and params["repos"] == [PREPO] and params["sev"] == ["medium"]
    )


# ---- the router --------------------------------------------------------------------


class _Src:
    """A fake TrackerSource recording every reader call with its keyword arguments."""

    def __init__(self, *, restricted: bool) -> None:
        self.cfg = type("C", (), {"restricted": restricted})()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name):
        async def f(*a, **k):
            self.calls.append((name, a, dict(k)))
            if name == "open_issues_by_component":
                sev = k.get("severities") or ["medium", "low"]
                return [
                    {
                        "number": 1 + i,
                        "title": f"t{i}",
                        "severity_level": s,
                        "state": "open",
                        "opened_at": "2026-09-01T00:00:00",
                    }
                    for i, s in enumerate(sev)
                ]
            if name == "component_risk_latest":
                return {
                    "MergeTree": {
                        "component": "MergeTree",
                        "total_bugs": 1,
                        "open_bugs": 1,
                        "gap_ratio": 0.5,
                        "risk_score": 1.0,
                    }
                }
            if name == "incidents_in_area":
                return [
                    {
                        "source_id": "acme/core-incidents#9",
                        "title": "Logical error",
                        "severity": "critical",
                        "error_type": "LOGICAL_ERROR",
                        "open": True,
                        "created_at": "2026-09-01T00:00:00",
                    }
                ]
            return []

        return f


@pytest.fixture
def wired(monkeypatch):
    holder: dict[str, Any] = {}
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: holder["src"])

    def _use(s: _Src) -> _Src:
        holder["src"] = s
        return s

    return _use


@pytest.mark.asyncio
async def test_the_dashboards_cell_read_is_one_filtered_statement_titled_for_the_cell(
    wired,
) -> None:
    s = wired(_Src(restricted=False))
    # the dashboard's cell read: anchors, no prose, the API's limit 50
    ans = await router.answer(
        "",
        org=ORG,
        tracker_namespace=PUB,
        limit=50,
        anchors={"components": ["MergeTree"], "severities": ["medium"], "words": ["risk"]},
    )
    by = {x.template: x for x in ans.sections}
    call = next(c for c in s.calls if c[0] == "open_issues_by_component")
    assert call[1] == (ORG, PUB, "MergeTree")
    assert call[2] == {"limit": cat.CELL_LIMIT, "severities": ["medium"]}
    sec = by["area_open_issues"]
    assert sec.title == "Open medium issues in MergeTree" and sec.status == "served"
    assert sec.params["severities"] == ["medium"] and sec.params["limit"] == cat.CELL_LIMIT
    d = ans.as_dict()
    assert (
        "area_open_issues(component='MergeTree', severities=['medium'], limit=300)" in d["searched"]
    )
    assert by["area_incidents"].status == "restricted_required"
    assert by["area_escalations"].status == "restricted_required"
    lines = router.render_lines(ans)
    assert any(
        ln.startswith("Open medium issues in MergeTree (1 shown; searched area_open_issues)")
        for ln in lines
    )
    # without severities: the plain read, the template's title, the request's limit
    s = wired(_Src(restricted=False))
    ans = await router.answer("MergeTree", org=ORG, tracker_namespace=PUB, limit=10)
    call = next(c for c in s.calls if c[0] == "open_issues_by_component")
    assert call[2] == {"limit": 10}  # no severities keyword: older fakes and readers stay callable
    assert next(x for x in ans.sections if x.template == "area_open_issues").title == (
        "Open issues in this component"
    )


@pytest.mark.asyncio
async def test_two_severities_title_both_and_the_restricted_class_is_served_on_its_namespace(
    wired,
) -> None:
    s = wired(_Src(restricted=True))
    ans = await router.answer(
        "open critical and medium bugs in MergeTree", org=ORG, tracker_namespace=PRIV
    )
    by = {x.template: x for x in ans.sections}
    assert ans.restricted
    assert by["area_open_issues"].title == "Open critical / medium issues in MergeTree"
    assert next(c for c in s.calls if c[0] == "open_issues_by_component")[2]["severities"] == [
        "critical",
        "medium",
    ]
    # the restricted class is rows here — served or an honest empty, never restricted_required
    assert by["area_incidents"].status == "served" and by["area_incidents"].rows[0]["title"] == (
        "Logical error"
    )
    assert by["area_escalations"].status == "empty"
    assert router._row_line("area_incidents", by["area_incidents"].rows[0]).startswith(
        "acme/core-incidents#9 · critical · LOGICAL_ERROR (open, 2026-09-01)"
    )
