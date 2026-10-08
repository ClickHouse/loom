"""tracker.item / tracker.release / tracker.component / tracker.flaky:
live keyed reads on the addressed tracker namespace, empty answers for
unknowns, `tracker_unavailable` when the source cannot answer, experimental and
VIEWER, no code-namespace defaulting."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.tracker.api.models import (
    TrackerComponentInput,
    TrackerFlakyInput,
    TrackerItemInput,
    TrackerReleaseInput,
)
from server.tracker.app import handlers as h
from server.tracker.app import source as tsrc
from server.tracker.domain.errors import TrackerUnavailable

REPO = "ClickHouse/ClickHouse"
ORG, NS = "clickhouse", "tracker-clickhouse"


class _Repo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def items_by_number(self, org, ns, nums):
        self.calls.append(("items", (ns, nums)))
        return {
            100: {
                "repo": REPO,
                "number": 100,
                "is_pr": 0,
                "state": "open",
                "title": "Keeper crash",
                "author": "u",
                "primary_component": "Keeper",
                "severity_level": "major",
                "labels": ["bug"],
                "affected_versions": ["26.4"],
                "bisect_outcome": "CONVERGED",
                "reproduces_on": ["26.4"],
                "clean_on": [],
                "introduced_commit": "abc",
                "opened_at": datetime(2026, 9, 1),
                "url": "u/100",
            }
        }

    async def edges_from(self, org, ns, refs, rels=None):
        self.calls.append(("edges", refs))
        return [
            {
                "src_ref": f"{REPO}#100",
                "rel_type": "fixes",
                "dst_ref": f"{REPO}#200",
                "direction": "in",
            }
        ]

    async def release_items(self, org, ns, version):
        self.calls.append(("release", version))
        return [
            {
                "item_kind": "blocker",
                "item_ref": f"{REPO}#5",
                "status": "Blockers",
                "issue_state": "OPEN",
                "fix_version": "",
            },
            {
                "item_kind": "blocker",
                "item_ref": f"{REPO}#6",
                "status": "Merged",
                "issue_state": "CLOSED",
                "fix_version": "26.3.2",
            },
            {
                "item_kind": "health",
                "item_ref": "health",
                "open_blockers": 1,
                "critical_untested": 0,
                "total_items": 9,
                "snapshot_date": datetime(2026, 9, 15).date(),
            },
        ]

    async def component_risk_latest(self, org, ns, comps):
        self.calls.append(("risk", comps))
        return {
            "MergeTree": {
                "component": "MergeTree",
                "gap_ratio": 0.25,
                "risk_score": 0.7,
                "snapshot_date": datetime(2026, 9, 15).date(),
            }
        }

    async def open_issues_by_component(self, org, ns, component, *, limit=10):
        self.calls.append(("open", component))
        return [
            {
                "number": 7,
                "title": "t",
                "severity_level": "critical",
                "labels": [],
                "affected_versions": [],
                "bisect_outcome": "",
                "reproduces_on": [],
                "is_flaky_test": 0,
                "is_crash_ci": 1,
                "opened_at": datetime(2026, 9, 2),
                "url": "u/7",
            }
        ]

    async def flaky_issues_matching(self, org, ns, name, *, limit=10):
        self.calls.append(("flaky", name))
        if "02345" not in name:
            return []
        return [
            {
                "number": 9,
                "title": f"{name} is flaky",
                "state": "open",
                "state_reason": "",
                "labels": ["flaky test"],
                "opened_at": datetime(2026, 9, 3),
                "closed_at": None,
                "url": "u/9",
            }
        ]


@pytest.fixture
def repo(monkeypatch) -> _Repo:
    r = _Repo()
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: r)
    return r


class _Down:
    def __getattr__(self, name):
        async def _raise(*a, **k):
            raise TrackerUnavailable("breaker OPEN")

        return _raise


@pytest.mark.asyncio
async def test_unavailable_source_is_marked_never_empty(monkeypatch) -> None:
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Down())
    item = await h.handle_tracker_item(TrackerItemInput(org=ORG, namespace=NS, numbers=[100]))
    assert item.tracker_unavailable and item.items == [] and item.unknown == []
    rel = await h.handle_tracker_release(TrackerReleaseInput(org=ORG, namespace=NS, version="26.3"))
    assert rel.tracker_unavailable and rel.blockers == []
    comp = await h.handle_tracker_component(
        TrackerComponentInput(org=ORG, namespace=NS, component="X")
    )
    assert comp.tracker_unavailable and not comp.known
    fl = await h.handle_tracker_flaky(TrackerFlakyInput(org=ORG, namespace=NS, test_name="02345_x"))
    assert fl.tracker_unavailable and fl.verdict == "tracker_unavailable" and not fl.known_flaky


def test_ops_are_registered_experimental_viewer_reads() -> None:
    for name in ("tracker.item", "tracker.release", "tracker.component", "tracker.flaky"):
        op = OPERATIONS_BY_NAME[name]
        assert op.required_role == Role.VIEWER and op.maturity == "experimental"
        assert op.idempotent and not op.skip_namespace_authz
        assert (
            "namespace" in op.input_model.model_fields
        )  # the tracker namespace is the authz target


@pytest.mark.asyncio
async def test_tracker_item_returns_facts_and_names_unknowns(repo) -> None:
    out = await h.handle_tracker_item(
        TrackerItemInput(org=ORG, namespace=NS, numbers=[100, 999, 0])
    )
    assert [i["number"] for i in out.items] == [100] and out.unknown == [999]
    item = out.items[0]
    assert item["tracker"]["fixed_by"] == [200] and item["line"].startswith(
        "[tracker] #100: component Keeper"
    )
    assert repo.calls[0] == ("items", (NS, [100, 999]))


@pytest.mark.asyncio
async def test_tracker_release_sorts_active_blockers_first(repo) -> None:
    out = await h.handle_tracker_release(TrackerReleaseInput(org=ORG, namespace=NS, version="26.3"))
    assert out.active_blockers == 1 and out.blockers[0]["ref"] == f"{REPO}#5"
    assert out.blockers[1]["active"] is False and out.health["open_blockers"] == 1


@pytest.mark.asyncio
async def test_tracker_component_reads_risk_and_open_issues(repo) -> None:
    out = await h.handle_tracker_component(
        TrackerComponentInput(org=ORG, namespace=NS, component="MergeTree")
    )
    assert out.known and out.risk["gap_ratio"] == 0.25 and out.risk["snapshot_date"] == "2026-09-15"
    assert out.open_issues[0]["number"] == 7 and out.open_issues[0]["is_crash_ci"] is True


@pytest.mark.asyncio
async def test_tracker_flaky_is_silent_without_signal(repo) -> None:
    out = await h.handle_tracker_flaky(
        TrackerFlakyInput(org=ORG, namespace=NS, test_name="02345_keeper_session")
    )
    assert out.known_flaky and out.verdict == "flaky_by_issue" and out.open_issues == 1
    out = await h.handle_tracker_flaky(
        TrackerFlakyInput(org=ORG, namespace=NS, test_name="00001_other")
    )
    assert not out.known_flaky and out.verdict == "no_flaky_signal" and out.issues == []
