"""The live tracker source: a standard namespace reads with the
public credential and `is_private = 0` on every statement; a restricted one
reads with the private credential and only while its directory row says
restricted; the budget, the breaker and the kill switch are one
`TrackerUnavailable`; rows come back in the tracker row shape; the cache pays once."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

import server.identity.app.namespace_classification as ncls
import server.tracker.domain.namespaces as qn
import server.tracker.store.source as src
import server.tracker.store.source.base as src_base
import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from server.tracker.domain.errors import TrackerUnavailable
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
    """Records every statement with its credential; answers canned rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.rows = rows or []
        self.delay = delay

    async def __call__(self, sql, params=None, *, credential="qa", **kw):
        self.calls.append((" ".join(sql.split()), dict(params or {}), credential))
        if self.delay:
            await asyncio.sleep(self.delay)
        return list(self.rows)


def _issue_upstream() -> dict[str, Any]:
    return {
        "kind": "issue",
        "source_id": f"{REPO}#100",
        "url": "u/100",
        "title": "Keeper crash",
        "author": "u",
        "labels": ["bug"],
        "assignees": [],
        "primary_component": "Keeper",
        "severity_level": "major",
        "is_private": 0,
        "created_at": datetime(2026, 9, 1),
        "updated_at": datetime(2026, 9, 2),
        "closed_at": datetime(1970, 1, 1),
        "state_reason": "",
        "affected_versions": ["26.4"],
        "is_flaky_test": 0,
        "is_crash_ci": 1,
        "is_performance": 0,
        "triage_bisect_outcome": "CONVERGED",
        "triage_reproduces_on": ["26.4"],
        "triage_clean_on": ["master"],
        "triage_introduced_commit": "abcdef1234567890",
        "triage_component": "Keeper",
        "triage_confidence": "HIGH",
    }


def test_namespace_config_inverts_the_allowlist() -> None:
    cfg = qn.configured_namespaces()
    assert cfg[PUB].repos == (REPO,) and not cfg[PUB].restricted and cfg[PUB].credential == "qa"
    assert cfg[PRIV].restricted and cfg[PRIV].credential == "qa" and cfg[PRIV].is_private == 1
    assert qn.tracker_namespace(ORG, "nope") is None and qn.tracker_namespace("other", PUB) is None


@pytest.mark.asyncio
async def test_standard_namespace_reads_public_with_is_private_zero(monkeypatch) -> None:
    up = _Upstream([_issue_upstream()])
    monkeypatch.setattr(qi, "query_rows", up)
    rows = await src.TrackerSource(ORG, PUB).items_by_number(ORG, PUB, [100, 0, 7])
    assert list(rows) == [100]
    row = rows[100]
    assert row["is_pr"] == 0 and row["state"] == "open" and row["closed_at"] is None
    assert row["bisect_outcome"] == "CONVERGED" and row["reproduces_on"] == ["26.4"]
    assert row["is_crash_ci"] == 1 and row["repo"] == REPO
    # two key reads side by side (the issue and PR column lists differ in width,
    # so one UNION ALL is refused upstream)
    assert len(up.calls) == 2 and {c[0].split(" FROM ")[1].split(" ")[0] for c in up.calls} == {
        "qa_intelligence.github_issues",
        "qa_intelligence.github_prs",
    }
    for sql, params, cred in up.calls:
        assert cred == "qa" and sql.count("is_private = 0 AND") == 1
        assert "FINAL" in sql and params["refs"] == [f"{REPO}#7", f"{REPO}#100"]
    pr_sql = next(c[0] for c in up.calls if "github_prs" in c[0])
    assert "CAST([] AS Array(String)) AS labels" in pr_sql  # github_prs has no labels column


@pytest.mark.asyncio
async def test_restricted_namespace_needs_private_credential_and_restricted_row(
    monkeypatch,
) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)

    async def _restricted(org, ns):
        return ns == PRIV

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    await src.TrackerSource(ORG, PRIV).items_by_number(ORG, PRIV, [5])
    assert len(up.calls) == 2
    for sql, _, cred in up.calls:
        assert cred == "qa" and "is_private = 0" not in sql

    async def _standard(org, ns):
        return False

    monkeypatch.setattr(ncls, "is_restricted", _standard)
    src.reset_cache_for_tests()
    with pytest.raises(TrackerUnavailable, match="directory row is not"):
        await src.TrackerSource(ORG, PRIV).items_by_number(ORG, PRIV, [6])
    assert len(up.calls) == 2  # nothing was read

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    # one credential for every namespace: unset = unavailable everywhere
    monkeypatch.setattr(settings, "tracker_credentials_json", "")
    with pytest.raises(TrackerUnavailable, match="credential 'qa'"):
        await src.TrackerSource(ORG, PRIV).items_by_number(ORG, PRIV, [6])
    assert len(up.calls) == 2


@pytest.mark.asyncio
async def test_unknown_namespace_kill_switch_and_breaker_are_unavailable(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    with pytest.raises(TrackerUnavailable, match="not a configured tracker namespace"):
        await src.TrackerSource(ORG, "tracker-other").items_by_number(ORG, "tracker-other", [1])
    monkeypatch.setattr(settings, "tracker_live_serve", False)
    with pytest.raises(TrackerUnavailable, match="LOOM_TRACKER_LIVE_SERVE"):
        await src.TrackerSource(ORG, PUB).items_by_number(ORG, PUB, [1])
    monkeypatch.setattr(settings, "tracker_live_serve", True)

    async def _open(sql, params=None, *, credential="qa", **kw):
        raise qi.QaIntelUnavailable("qa_intel[public] breaker is OPEN")

    monkeypatch.setattr(qi, "query_rows", _open)
    with pytest.raises(TrackerUnavailable, match="OPEN"):
        await src.TrackerSource(ORG, PUB).items_by_number(ORG, PUB, [1])
    assert up.calls == []


@pytest.mark.asyncio
async def test_budget_exceeded_is_unavailable_but_not_a_breaker_failure(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_live_budget_ms", 50)
    monkeypatch.setattr(qi, "query_rows", _Upstream([], delay=0.5))
    with pytest.raises(TrackerUnavailable, match="exceeded 50 ms"):
        await src.TrackerSource(ORG, PUB).items_by_number(ORG, PUB, [1])
    # both key reads of the item lookup time out — the caller's deadline, not
    # the upstream's outage: the breaker stays closed (under a
    # brief's fan-out three such timeouts would open it and refuse every point
    # read behind them)
    assert qi.circuit_status("qa") == {"state": "CLOSED", "consecutive_failures": 0}


@pytest.mark.asyncio
async def test_cache_pays_once_and_empty_results_expire_sooner(monkeypatch) -> None:
    up = _Upstream([_issue_upstream()])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    await s.items_by_number(ORG, PUB, [100])
    await s.items_by_number(ORG, PUB, [100])
    assert len(up.calls) == 2  # one pass of two key reads, the second call served from cache
    up.rows = []
    await s.items_by_number(ORG, PUB, [101])
    key = next(k for k in src_base._cache if "|items_issues|" in k and "101" in k)
    ttl_empty = src_base._cache[key][0]
    key_full = next(k for k in src_base._cache if "|items_issues|" in k and "100" in k)
    assert src_base._cache[key_full][0] - ttl_empty > 100  # 300 s vs ≤ 60 s


@pytest.mark.asyncio
async def test_edges_resolve_both_directions_and_drop_unseen_ends(monkeypatch) -> None:
    calls: list[str] = []

    # our end's id is derived locally: uuid5(NAMESPACE_DNS, "github_pr:<ref>")
    me = qn.doc_id("github_pr", f"{REPO}#200")

    async def _q(sql, params=None, *, credential="qa", **kw):
        calls.append(" ".join(sql.split()))
        if "FROM qa_intelligence.edges" in sql:
            assert me in params["ids"] and qn.doc_id("github_issue", f"{REPO}#200") in params["ids"]
            return [
                {
                    "rel_type": "fixes",
                    "from_type": "github_pr",
                    "to_type": "github_issue",
                    "from_id": me,
                    "to_id": "id-100",
                    "created_at": datetime(2026, 9, 3),
                },
                {
                    "rel_type": "caused_by",
                    "from_type": "github_issue",
                    "to_type": "github_pr",
                    "from_id": "id-300",
                    "to_id": me,
                    "created_at": datetime(2026, 9, 4),
                },
                {
                    "rel_type": "fixes",
                    "from_type": "github_pr",
                    "to_type": "github_issue",
                    "from_id": me,
                    "to_id": "id-private",
                    "created_at": datetime(2026, 9, 5),
                },
                {
                    "rel_type": "fixes",
                    "from_type": "github_pr",
                    "to_type": "github_issue",
                    "from_id": "someone-else",
                    "to_id": "id-100",
                    "created_at": datetime(2026, 9, 6),
                },
            ]
        return [
            {"id": "id-100", "source_id": f"{REPO}#100"},
            {"id": "id-300", "source_id": f"{REPO}#300"},
        ]

    monkeypatch.setattr(qi, "query_rows", _q)
    edges = await src.TrackerSource(ORG, PUB).edges_from(
        ORG, PUB, [f"{REPO}#200"], ["fixes", "caused_by"]
    )
    assert [(e["rel_type"], e["dst_ref"], e["direction"]) for e in edges] == [
        ("caused_by", f"{REPO}#300", "in"),
        ("fixes", f"{REPO}#100", "out"),
    ]
    assert edges[0]["src_kind"] == "github_pr" and edges[0]["dst_kind"] == "github_issue"
    assert len(calls) == 2 and "rel_type IN {rels:Array(String)}" in calls[0]
    assert "from_id IN {ids:Array(UUID)} OR to_id IN {ids:Array(UUID)}" in calls[0]
    assert "FROM qa_intelligence.github_issues" not in calls[0]  # no lookup of our own end
    # the other ends resolve in the tables their types name, with the privacy predicate
    assert "is_private = 0 AND id IN {ids:Array(UUID)}" in calls[1]
    # every other end here is typed github_issue: one table read, not five
    assert calls[1].count("UNION ALL") == 0 and "github_prs" not in calls[1]


@pytest.mark.asyncio
async def test_flaky_and_component_reads_scope_to_the_namespace_repos(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    assert await s.flaky_issues_matching(ORG, PUB, "02345_Keeper-session", limit=99) == []
    sql, params, _ = up.calls[-1]
    # one index-served hasAnyTokens per token, every spelling (the upstream index is case-sensitive)
    assert (
        "hasAnyTokens(title, {t0:Array(String)}) AND hasAnyTokens(title, {t1:Array(String)})" in sql
    )
    assert params["t1"] == ["KEEPER", "Keeper", "keeper"] and params["t0"] == ["02345"]
    assert "hasAllTokens" not in sql and "lower(title)" not in sql
    assert params["repos"] == [REPO] and params["limit"] == 50 and "is_flaky_test = 1" in sql
    assert await s.open_issues_by_component(ORG, PUB, "MergeTree") == []
    sql, params, _ = up.calls[-1]
    assert "closed_at <= toDateTime64(0, 3)" in sql and params["component"] == "MergeTree"
    risk = await s.component_risk_latest(ORG, PUB, ["MergeTree"])
    assert risk == {} and "LIMIT 1 BY component" in up.calls[-1][0]
    rel = await s.release_items(ORG, PUB, "26.3")
    assert rel == [] and up.calls[-1][1] == {"v": "26.3", "repos": [REPO]}


def test_pr_row_shape_matches_the_facts_contract() -> None:
    row = src.pr_row(
        {
            "source_id": f"{REPO}#200",
            "pr_number": 200,
            "state": "merged",
            "is_merged": 1,
            "has_test": None,
            "reviewers": ["a"],
            "closed_at": datetime(2026, 9, 9),
            "created_at": datetime(2026, 9, 1),
            "is_backport_pr": 1,
            "upstream_pr_number": 150,
        }
    )
    assert row["is_pr"] == 1 and row["has_test"] == -1 and row["is_merged"] == 1
    assert row["state"] == "merged" and row["upstream_pr_number"] == 150
    from server.tracker.app.facts import fact_of

    fact = fact_of(row, [])
    assert fact["kind"] == "pr" and "has_test" not in fact and fact["is_backport_pr"] is True


@pytest.mark.asyncio
async def test_bisect_candidates_scan_the_diff_once_per_row(monkeypatch) -> None:
    """Needles are searched in one pass (multiSearchAllPositions,
    multiMatchAllIndices), never one position()/match() per needle; an empty
    needle list is replaced by one that cannot occur, so the statement keeps
    its shape; directory widening adds the startsWith scope."""
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    await s.merged_prs_touching(
        ORG,
        PUB,
        ["src/a.cpp"],
        ["26.4"],
        symbols=["loadDataParts"],
        patterns=["apply_patch_parts"],
        dirs=["src/Storages/MergeTree/"],
    )
    sql, params, _ = up.calls[-1]
    assert sql.count("multiSearchAllPositions(diff") == 2 and "multiMatchAllIndices(diff" in sql
    assert "position(diff" not in sql and "match(diff" not in sql
    assert params["added_res"] == [r"(?m)^\+[^\n]*apply_patch_parts"] and params["pats"] == [
        "apply_patch_parts"
    ]
    assert "arrayExists(d -> startsWith(f, d), {dirs:Array(String)})" in sql
    assert params["dirs"] == ["src/Storages/MergeTree/"]
    await s.merged_prs_touching(ORG, PUB, ["src/a.cpp"], ["26.4"])
    sql, params, _ = up.calls[-1]
    assert params["names"] == ["\x00loom-no-needle"] and params["pats"] == ["\x00loom-no-needle"]
    assert params["added_res"] == [] and "arrayExists" not in sql
    assert await s.issue_text(ORG, PUB, 0) == ""
    await s.issue_text(ORG, PUB, 100)
    sql, params, _ = up.calls[-1]
    assert "substring(body, 1, {chars:UInt32})" in sql and params["refs"] == [f"{REPO}#100"]


@pytest.mark.asyncio
async def test_graph_read_runs_under_the_scan_budget(monkeypatch) -> None:
    """`edges` is keyed (rel_type, from_id, to_id) but a read
    by our ids scans hundreds of thousands of rows; under the 300 ms
    point-read budget it would time out on every bisect call and drop the QA
    half of symbol_history, blame_range and the brief. Both statements of `edges_from` — the edges and the other ends —
    carry the scan budget, and a read slower than the point budget but
    inside the scan budget is served."""
    monkeypatch.setattr(settings, "tracker_live_budget_ms", 100)
    monkeypatch.setattr(settings, "tracker_live_text_budget_ms", 900)
    me = qn.doc_id("github_pr", f"{REPO}#200")
    seen: list[tuple[str, float | None]] = []

    async def _q(sql, params=None, *, credential="qa", max_execution_s=None, **kw):
        seen.append((" ".join(sql.split()), max_execution_s))
        await asyncio.sleep(0.15)  # past the point budget, inside the scan budget
        if "FROM qa_intelligence.edges" in sql:
            return [
                {
                    "rel_type": "fixes",
                    "from_type": "github_pr",
                    "to_type": "github_issue",
                    "from_id": me,
                    "to_id": "id-100",
                    "created_at": datetime(2026, 9, 3),
                }
            ]
        return [{"id": "id-100", "source_id": f"{REPO}#100"}]

    monkeypatch.setattr(qi, "query_rows", _q)
    edges = await src.TrackerSource(ORG, PUB).edges_from(ORG, PUB, [f"{REPO}#200"], ["fixes"])
    assert [(e["rel_type"], e["dst_ref"]) for e in edges] == [("fixes", f"{REPO}#100")]
    assert len(seen) == 2 and all(budget == 0.9 for _, budget in seen)
    assert "FROM qa_intelligence.edges" in seen[0][0] and "id IN {ids:Array(UUID)}" in seen[1][0]
    # the point reads keep the point budget: a number lookup is not a scan
    src.reset_cache_for_tests()
    seen.clear()
    with pytest.raises(TrackerUnavailable, match="exceeded 100 ms"):
        await src.TrackerSource(ORG, PUB).items_by_number(ORG, PUB, [200])
    assert seen and all(budget is None for _, budget in seen)
    # and the scan budget is a ceiling too
    src.reset_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_live_text_budget_ms", 50)
    with pytest.raises(TrackerUnavailable, match="edges exceeded 50 ms"):
        await src.TrackerSource(ORG, PUB).edges_from(ORG, PUB, [f"{REPO}#200"], ["fixes"])


@pytest.mark.asyncio
async def test_prs_facts_reads_the_diff_under_the_scan_budget(monkeypatch) -> None:
    """`prs_facts` with names scans the diff column of every
    keyed row (hundreds of MB for a few dozen PRs); it carries the scan budget. Without
    names it is a point read and keeps the point budget."""
    monkeypatch.setattr(settings, "tracker_live_budget_ms", 300)
    monkeypatch.setattr(settings, "tracker_live_text_budget_ms", 900)
    seen: list[tuple[str, float | None]] = []

    async def _q(sql, params=None, *, credential="qa", max_execution_s=None, **kw):
        seen.append((" ".join(sql.split()), max_execution_s))
        return [
            {
                "source_id": f"{REPO}#7",
                "pr_number": 7,
                "has_test": 1,
                "ci_result": "success",
                "minor_version": "26.9",
                "is_backport_pr": 0,
                "is_merged": 1,
                "closed_at": datetime(2026, 9, 1),
                "matched_symbols": ["loadDataParts"],
            }
        ]

    monkeypatch.setattr(qi, "query_rows", _q)
    facts = await src.TrackerSource(ORG, PUB).prs_facts(ORG, PUB, [7], names=["loadDataParts"])
    assert facts[7]["matched_symbols"] == ["loadDataParts"] and seen[-1][1] == 0.9
    assert "is_private = 0 AND" in seen[-1][0] and "position(diff, s)" in seen[-1][0]
    src.reset_cache_for_tests()
    await src.TrackerSource(ORG, PUB).prs_facts(ORG, PUB, [7])
    assert seen[-1][1] is None
    # the whole 120-PR window of symbol_history is settled in one read (cap 128)
    src.reset_cache_for_tests()
    await src.TrackerSource(ORG, PUB).prs_facts(ORG, PUB, list(range(1, 131)), names=["x"])
    assert len(seen[-1][0]) and seen[-1][0].count("{refs:Array(String)}") == 1
