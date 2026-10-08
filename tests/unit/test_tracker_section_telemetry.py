"""Every tracker.answer section is recorded once — template,
status, restricted flag and the read's wall time — and the recorder can
never touch the answer."""

from __future__ import annotations

from typing import Any

import pytest

import server.identity.app.namespace_classification as ncls
import server.observability.metrics as metrics
import server.tracker.app.router as router
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from server.tracker.app import source as tsrc
from server.tracker.domain.errors import TrackerUnavailable
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, PUB, PRIV = "clickhouse", "tracker-clickhouse", "tracker-example-private"
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

    async def _restricted(org, ns):
        return ns == PRIV

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


class _Src:
    def __init__(self, *, restricted: bool) -> None:
        self.cfg = type("C", (), {"restricted": restricted})()

    async def component_risk_latest(self, org, ns, comps):
        return {
            "MergeTree": {
                "component": "MergeTree",
                "total_bugs": 4,
                "open_bugs": 1,
                "gap_ratio": 0.2,
                "risk_score": 1.0,
            }
        }

    async def open_issues_by_component(self, org, ns, component, *, limit=10):
        return []

    async def incidents_in_area(self, org, ns, **kw):
        raise TrackerUnavailable("read exceeded 300 ms")

    async def escalations_in_area(self, org, ns, **kw):
        return [{"source_id": f"{REPO}#1", "title": "t"}]


@pytest.mark.asyncio
async def test_every_section_is_recorded_with_status_and_time(monkeypatch) -> None:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(metrics, "record_tracker_section", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Src(restricted=True))
    ans = await router.answer(
        "Core incidents on MergeTree in the last 30 days", org=ORG, tracker_namespace=PRIV
    )
    by = {r["template"]: r for r in recorded}
    assert (
        by["component_risk"]["status"] == "served" and by["area_open_issues"]["status"] == "empty"
    )
    assert (
        by["area_incidents"]["status"] == "tracker_unavailable"
        and by["area_escalations"]["status"] == "served"
    )
    assert all(r["restricted"] is True for r in recorded) and len(recorded) == len(ans.sections)
    assert all(r["ms"] >= 0 for r in recorded)
    # the answer carries each section's time
    d = ans.as_dict()
    assert all("ms" in s for s in d["sections"])


@pytest.mark.asyncio
async def test_restricted_required_is_recorded_without_a_read(monkeypatch) -> None:
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(metrics, "record_tracker_section", lambda **kw: recorded.append(kw))
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Src(restricted=False))
    ans = await router.answer(
        "Core incidents on MergeTree in the last 30 days", org=ORG, tracker_namespace=PUB
    )
    by = {r["template"]: r for r in recorded}
    assert (
        by["area_incidents"]["status"] == "restricted_required"
        and by["area_incidents"]["ms"] == 0.0
    )
    assert by["area_incidents"]["restricted"] is False
    assert {s.template: s.status for s in ans.sections}["area_incidents"] == "restricted_required"


@pytest.mark.asyncio
async def test_a_failing_recorder_never_touches_the_answer(monkeypatch) -> None:
    def _boom(**kw):
        raise RuntimeError("meter gone")

    monkeypatch.setattr(metrics, "record_tracker_section", _boom)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Src(restricted=True))
    ans = await router.answer(
        "Core incidents on MergeTree in the last 30 days", org=ORG, tracker_namespace=PRIV
    )
    assert {s.template: s.status for s in ans.sections}["component_risk"] == "served"


def test_recorder_is_a_no_op_before_the_meter_exists() -> None:
    metrics.record_tracker_section(template="item", status="served", ms=12.0, restricted=False)
