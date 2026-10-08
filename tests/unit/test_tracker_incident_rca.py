"""An incident's recorded root cause (the RCA columns) is
read with the head row and rendered first; escalations, which have no RCA
columns, are read without them."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.identity.app.namespace_classification as ncls
import server.tracker.app.router as router
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, PUB, PRIV = "clickhouse", "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"
INC = "acme/core-incidents#2124"


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
    monkeypatch.setattr(
        settings,
        "tracker_restricted_repos_json",
        '{"escalation": "acme/escalations", "incident": "acme/core-incidents"}',
    )

    async def _restricted(org, ns):
        return ns == PRIV

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


def _head(rca: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "x",
        "source_id": INC,
        "url": "u",
        "title": "[CRASH] Reader cache",
        "severity_level": "critical",
        "primary_component": "Formats",
        "created_at": datetime(2026, 9, 10),
        "closed_at": datetime(1970, 1, 1),
        "excerpt": "trace",
    }
    if rca:
        row.update(
            {
                "rca_confidence": "high",
                "rca_caused_by_prs": [],
                "rca_fix_prs": [f"{PREPO}#4242"],
                "rca_backport_pending": ["26.8", "26.7"],
                "rca_affected_versions": ["26.6.1.2047"],
                "rca_same_root_cause": ["acme/core-incidents#2116"],
                "rca_duplicates_of": ["acme/core-incidents#2034"],
                "rca_notes": "Reader cache use-after-free.",
            }
        )
    return row


@pytest.mark.asyncio
async def test_incident_head_reads_the_rca_columns_and_the_context_carries_them(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _q(sql, params=None, *, credential="qa", **kw):
        calls.append((" ".join(sql.split()), dict(params or {})))
        if "FROM qa_intelligence.core_incidents FINAL WHERE source_id" in sql:
            return [_head(rca=True)]
        return []

    monkeypatch.setattr(qi, "query_rows", _q)
    s = src.TrackerSource(ORG, PRIV)
    ctx = await s.incident_context(ORG, PRIV, number=2124)
    head_sql = next(q for q, _ in calls if "core_incidents FINAL WHERE source_id" in q)
    assert "rca_confidence, rca_caused_by_prs, rca_fix_prs, rca_backport_pending" in head_sql
    assert "substring(rca_notes, 1, 600) AS rca_notes" in head_sql
    assert ctx["rca"] == {
        "confidence": "high",
        "caused_by_prs": [],
        "fix_prs": [f"{PREPO}#4242"],
        "backport_pending": ["26.8", "26.7"],
        "affected_versions": ["26.6.1.2047"],
        "same_root_cause": ["acme/core-incidents#2116"],
        "duplicates_of": ["acme/core-incidents#2034"],
        "notes": "Reader cache use-after-free.",
    }
    assert ctx["doc"]["source_id"] == INC


@pytest.mark.asyncio
async def test_no_confidence_means_no_rca_and_escalations_read_no_rca_columns(monkeypatch) -> None:
    calls: list[str] = []

    async def _q(sql, params=None, *, credential="qa", **kw):
        calls.append(" ".join(sql.split()))
        if "core_incidents FINAL WHERE source_id" in sql:
            return [_head(rca=False)]
        if "support_cases FINAL WHERE source_id" in sql:
            return [{**_head(rca=False), "source_id": "acme/escalations#8676"}]
        return []

    monkeypatch.setattr(qi, "query_rows", _q)
    s = src.TrackerSource(ORG, PRIV)
    ctx = await s.incident_context(ORG, PRIV, number=2124)
    assert "rca" not in ctx and ctx["doc"]["source_id"] == INC
    ctx = await s.escalation_context(ORG, PRIV, number=8676)
    esc_sql = next(q for q in calls if "support_cases FINAL WHERE source_id" in q)
    assert "rca_" not in esc_sql and "rca" not in ctx


@pytest.mark.asyncio
async def test_a_number_names_the_record_in_the_configured_repository(monkeypatch) -> None:
    sids: list[str] = []

    async def _q(sql, params=None, *, credential="qa", **kw):
        if "FINAL WHERE source_id" in sql:
            sids.append(str((params or {}).get("sid")))
        return []

    monkeypatch.setattr(qi, "query_rows", _q)
    s = src.TrackerSource(ORG, PRIV)
    await s.escalation_context(ORG, PRIV, number=8676)
    await s.incident_context(ORG, PRIV, number=2124)
    assert sids == ["acme/escalations#8676", INC]

    monkeypatch.setattr(settings, "tracker_restricted_repos_json", "")
    sids.clear()
    assert await s.escalation_context(ORG, PRIV, number=8676) == {}
    assert await s.incident_context(ORG, PRIV, number=2124) == {}
    assert sids == []


def test_renderer_puts_the_root_cause_before_the_graph() -> None:
    sec = router.Section(
        template="incident_context",
        title="Incident, its lifecycle and the PRs that fix it",
        access="restricted",
        params={},
        why="",
        status="served",
        rows={
            "doc": {
                "source_id": INC,
                "severity": "critical",
                "component": "Formats",
                "title": "[CRASH] Reader cache",
                "closed_at": None,
            },
            "rca": {
                "confidence": "high",
                "caused_by_prs": [],
                "fix_prs": [f"{PREPO}#4242"],
                "backport_pending": ["26.8", "26.7"],
                "affected_versions": ["26.6.1.2047"],
                "same_root_cause": ["acme/core-incidents#2116"],
                "duplicates_of": [],
                "notes": "Reader cache use-after-free.",
            },
            "links": [
                {
                    "rel_type": "fixes",
                    "direction": "in",
                    "kind": "github_pr",
                    "source_id": f"{PREPO}#4242",
                    "title": "Fix use-after-free",
                }
            ],
            "instances": [],
        },
    )
    ans = router.TrackerAnswer(
        intent="incident_context", anchors={}, namespace=PRIV, sections=[sec]
    )
    lines = router.render_lines(ans)
    assert lines[1] == (
        "- root cause (high): Reader cache use-after-free.; fixed by ClickHouse/example-private#4242;"
        " backport pending 26.8, 26.7; crashed on 26.6.1.2047;"
        " same root cause as acme/core-incidents#2116"
    )
    assert lines[2].startswith("- fixes in github_pr")
