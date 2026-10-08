"""QA facts: the compact dict and the prose line for an
issue and a PR, the graph in both directions, silence for unknowns."""

from __future__ import annotations

import pytest

import server.tracker.app.facts as f

REPO = "ClickHouse/ClickHouse"


def _issue():
    return {
        "repo": REPO,
        "number": 100,
        "is_pr": 0,
        "primary_component": "Keeper",
        "severity_level": "major",
        "labels": ["bug", "crash"],
        "affected_versions": ["26.4"],
        "bisect_outcome": "CONVERGED",
        "reproduces_on": ["26.4", "26.5"],
        "clean_on": ["master"],
        "introduced_commit": "abcdef1234567890",
        "triage_confidence": "HIGH",
        "is_flaky_test": 0,
        "is_crash_ci": 0,
        "url": "https://github.com/x/100",
    }


def _pr():
    return {
        "repo": REPO,
        "number": 200,
        "is_pr": 1,
        "primary_component": "Keeper",
        "severity_level": "",
        "labels": [],
        "has_test": 0,
        "test_types": [],
        "ci_result": "success",
        "reviewers": ["dev-alice", "dev-bob"],
        "review_requests_test": 1,
        "changelog_category": "Bug Fix",
        "minor_version": "26.6",
        "is_backport_pr": 0,
        "upstream_pr_number": 0,
    }


def _edges():
    return [
        {
            "src_ref": f"{REPO}#200",
            "rel_type": "fixes",
            "dst_ref": f"{REPO}#100",
            "direction": "out",
        },
        {
            "src_ref": f"{REPO}#100",
            "rel_type": "fixes",
            "dst_ref": f"{REPO}#200",
            "direction": "in",
        },
        {
            "src_ref": f"{REPO}#300",
            "rel_type": "backports",
            "dst_ref": f"{REPO}#200",
            "direction": "out",
        },
        {
            "src_ref": f"{REPO}#200",
            "rel_type": "backports",
            "dst_ref": f"{REPO}#300",
            "direction": "in",
        },
        {
            "src_ref": f"{REPO}#100",
            "rel_type": "caused_by",
            "dst_ref": f"{REPO}#50",
            "direction": "out",
        },
    ]


def test_issue_fact_and_line() -> None:
    fact = f.fact_of(_issue(), _edges())
    assert fact["kind"] == "issue" and fact["component"] == "Keeper"
    assert fact["bisect_outcome"] == "CONVERGED" and fact["reproduces_on"] == ["26.4", "26.5"]
    assert fact["introduced_commit"] == "abcdef123456" and fact["fixed_by"] == [200]
    assert fact["caused_by"] == [50] and "is_flaky_test" not in fact
    line = f.tracker_fact_line(100, fact)
    assert line.startswith(
        "[tracker] #100: component Keeper; severity major; bisect CONVERGED (introduced at abcdef123456)"
    )
    assert "still reproduces on 26.4, 26.5" in line and "fixed by #200" in line


def test_pr_fact_and_line() -> None:
    fact = f.fact_of(_pr(), _edges())
    assert fact["kind"] == "pr" and fact["has_test"] is False and fact["ci_result"] == "success"
    assert fact["fixes"] == [100] and fact["backported_by"] == [300]
    assert fact["review_requests_test"] is True and "is_backport_pr" not in fact
    line = f.tracker_fact_line(200, fact)
    assert "adds no test" in line and "reviewers @dev-alice, @dev-bob" in line
    assert "fixes #100" in line and "backported by #300" in line


def test_unknown_test_presence_is_silent() -> None:
    row = _pr()
    row["has_test"] = -1
    fact = f.fact_of(row, [])
    assert "has_test" not in fact
    row["has_test"] = None
    assert "has_test" not in f.fact_of(row, [])


def test_empty_fact_renders_nothing() -> None:
    assert f.tracker_fact_line(5, {}) == ""
    assert f.tracker_fact_line(5, {"kind": "issue"}) == ""


@pytest.mark.asyncio
async def test_facts_for_numbers_reads_twice_and_fails_open(monkeypatch) -> None:
    calls = []

    class _Repo:
        def __init__(self, org=None, ns=None):
            pass

        async def items_by_number(self, org, ns, nums):
            calls.append(("items", ns, nums))
            return {100: _issue()}

        async def edges_from(self, org, ns, refs, rels):
            calls.append(("edges", refs, rels))
            return _edges()

    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", _Repo)
    facts = await f.tracker_facts_for_numbers("clickhouse", "tracker-clickhouse", [100, 999])
    assert set(facts) == {100} and facts[100]["fixed_by"] == [200]
    assert calls[0] == ("items", "tracker-clickhouse", [100, 999])
    assert calls[1][1] == [f"{REPO}#100"]
    assert await f.tracker_facts_for_numbers("clickhouse", "", [100]) == {}

    class _Down(_Repo):
        async def items_by_number(self, org, ns, nums):
            raise RuntimeError("down")

    monkeypatch.setattr(qs, "TrackerSource", _Down)
    assert await f.tracker_facts_for_numbers("clickhouse", "tracker-clickhouse", [100]) == {}
