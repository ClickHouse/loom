"""tracker.answer: the catalogue plans a question from its anchors
into named templates; the router runs them against one tracker namespace and
reports per section what it searched, `restricted_required` for incident /
escalation / customer / instance templates on a standard namespace,
`tracker_unavailable` when the source fails, `empty` for no rows; the readers
enforce the restricted class themselves and every statement is keyed or
index-served; memory.ask hands the shapes it does not know to the same
catalogue."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.identity.app.namespace_classification as ncls
import server.tracker.app.ask_route as ask
import server.tracker.app.router as router
import server.tracker.domain.namespaces as qn
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.api.models import TrackerAnswerInput
from server.tracker.app import handlers as h
from server.tracker.app import source as tsrc
from server.tracker.domain import catalogue as cat
from server.tracker.domain.errors import TrackerUnavailable
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, PUB, PRIV = "clickhouse", "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"
KC = ("MergeTree", "Keeper", "S3Queue")


# ---- the catalogue -------------------------------------------------------------


def _plan(q: str) -> cat.Plan:
    return cat.plan(q, known_components=KC)


def test_examples_plan_the_expected_templates() -> None:
    cases = {
        "Find all recent escalations for customer Acme Corp. Provide analytics over all channels.": (
            "customer_state",
            ["customer_state"],
        ),
        "Top 10 components by untested bug ratio this year": ("test_gaps", ["test_gaps"]),
        "Open 26.3 blockers that already have merged fixes, grouped by assignee": (
            "release_readiness",
            ["release_blockers"],
        ),
        "Core incidents on MergeTree in the last 30 days with no regression test": (
            "area_risk",
            ["area_incidents", "area_escalations", "component_risk", "area_open_issues"],
        ),
        "All Slack threads, issues and PRs related to escalation #41001": (
            "escalation_context",
            ["escalation_context"],
        ),
        "What did @jane-doe merge, review, or escalate this week?": (
            "person_activity",
            ["person_activity"],
        ),
        "PRs that fix incident INC-1234, and did any ship in a backport to 26.2?": (
            "incident_context",
            ["incident_context"],
        ),
        "Changelog entries in 26.3 mentioning Keeper": ("changelog", ["changelog"]),
        "PRs last week touching src/Storages/MergeTree/": ("prs_touching", ["prs_touching"]),
        "Issues mentioning 'S3Queue memory leak' with component or version context": (
            "similar_bugs",
            ["similar_bugs", "area_open_issues"],
        ),
        "production risks for S3Queue and which customers use it": (
            "customers_in_area",
            ["customers_in_area", "area_incidents", "area_escalations"],
        ),
        "is 02345_keeper_session flaky?": ("flaky", ["flaky"]),
        "what is PR #119663": ("item", ["item"]),
        "hello there": ("", []),
    }
    for q, (intent, templates) in cases.items():
        p = _plan(q)
        assert (p.intent, [s.template for s in p.steps]) == (intent, templates), q


def test_anchors_and_parameters_are_read_from_the_question() -> None:
    p = _plan("Core incidents on MergeTree in the last 30 days with no regression test")
    inc = p.steps[0].params
    assert inc["component"] == "MergeTree" and inc["days"] == 30 and inc["untested"] is True
    p = _plan("Find all recent escalations for customer Acme Corp. Provide analytics")
    assert p.steps[0].params["org_name"] == "Acme Corp" and p.steps[0].params["days"] == 365
    p = _plan("PRs that fix incident INC-1234, and did any ship in a backport to 26.2?")
    assert p.anchors.numbers == [(1234, "incident")] and p.anchors.versions == ["26.2"]
    p = _plan("Changelog entries in 26.3 mentioning Keeper")
    assert p.steps[0].params == {"version": "26.3", "term": "Keeper", "limit": 10}
    p = _plan("PRs last week touching src/Storages/MergeTree/")
    assert (
        p.steps[0].params["path_prefix"] == "src/Storages/MergeTree/"
        and p.steps[0].params["days"] == 14
    )
    p = _plan("What did @jane-doe merge this week?")
    assert p.steps[0].params["login"] == "jane-doe" and p.steps[0].params["days"] == 7
    for t in cat.TEMPLATES.values():
        assert t.access in (cat.PUBLIC, cat.RESTRICTED)
    assert {t.name for t in cat.TEMPLATES.values() if t.access == cat.RESTRICTED} == {
        "escalation_context",
        "incident_context",
        "customer_state",
        "area_incidents",
        "area_escalations",
        "customers_in_area",
        "slack_threads",
        "version_exposure",
        "version_limits",
    }


def test_a_number_takes_the_kind_of_the_nearest_whole_word() -> None:
    """A kind word inside another word ("problem", "MergeTree", "in case") names
    nothing; of several kind words, the one nearest the number decides."""
    cases = {
        "just in case, is issue 41040 fixed?": "issue",
        "problem with issue 41040": "issue",
        "MergeTree bug 41040": "issue",
        "a problem in 41040": "",
        "the issue fixed by PR 41040": "pr",
        "the pull request for escalation 41040": "escalation",
        "Incident 41040": "incident",
    }
    for q, kind in cases.items():
        assert cat.extract_anchors(q).numbers == [(41040, kind)], q
    assert "escalation_context" not in [
        s.template for s in _plan("just in case, is issue 41040 fixed?").steps
    ]


# ---- the readers enforce the class and keep the statement shape -------------


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG, raising=False)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(f'{{"{REPO}": "{PUB}", "{PREPO}": "{PRIV}"}}', restricted=PRIV),
        raising=False,
    )
    monkeypatch.setattr(
        settings, "tracker_credentials_json", tracker_credentials_json(), raising=False
    )
    monkeypatch.setattr(settings, "tracker_live_serve", True, raising=False)

    async def _restricted(org, ns):
        return ns == PRIV

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


class _Upstream:
    def __init__(self, rows=None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.rows = rows or []

    async def __call__(self, sql, params=None, *, credential="qa", **kw):
        self.calls.append((" ".join(sql.split()), dict(params or {}), credential))
        return list(self.rows)


@pytest.mark.asyncio
async def test_restricted_readers_refuse_a_standard_namespace_before_reading(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    for call in (
        lambda: s.incidents_in_area(ORG, PUB, component="MergeTree"),
        lambda: s.escalations_in_area(ORG, PUB, component="MergeTree"),
        lambda: s.customers_in_area(ORG, PUB, component="MergeTree"),
        lambda: s.escalation_context(ORG, PUB, number=1),
        lambda: s.incident_context(ORG, PUB, number=1),
        lambda: s.customer_state(ORG, PUB, org_name="Acme"),
    ):
        with pytest.raises(TrackerUnavailable, match="restricted tracker namespace"):
            await call()
    assert up.calls == []
    # public readers on the standard namespace carry the privacy predicate and the repo list
    await s.prs_touching_prefix(ORG, PUB, path_prefix="src/Storages/MergeTree/", days=14)
    sql, params, cred = up.calls[-1]
    assert cred == "qa" and "is_private = 0 AND is_merged = 1" in sql
    assert (
        params["prefix"] == "src/Storages/MergeTree/"
        and params["days"] == 14
        and params["repos"] == [REPO]
    )
    out = await s.similar_bugs(
        ORG, PUB, text="S3Queue memory leak", component="S3Queue", version="26.4"
    )
    sql, params, _ = up.calls[-2]  # the strict read: every token, title hits first
    assert params["sources"] == ["github_issue"] and params["t0"] == [
        "S3QUEUE",
        "S3Queue",
        "S3queue",
        "s3queue",
    ]
    assert params["t1"] == ["MEMORY", "Memory", "memory"] and params["t2"] == [
        "LEAK",
        "Leak",
        "leak",
    ]
    assert (
        "hasAnyTokens(title, {t0:Array(String)})" in sql
        and "hasAnyTokens(body, {t2:Array(String)})" in sql
    )
    # title hits rank first: body-only matches were the noise in the live read
    assert "ORDER BY (hasAnyTokens(title, {t0:Array(String)})" in sql and "hasAllTokens" not in sql
    # nothing carried every token: the most-tokens fallback ran, needing all but one
    sql, params, _ = up.calls[-1]
    assert ">= {need:UInt32}" in sql and params["need"] == 2 and "ORDER BY matched DESC" in sql
    assert "hasAnyTokens(title, {ta:Array(String)})" in sql and out == []
    assert (
        "has(affected_versions, {version:String})" in sql and "FROM qa_intelligence.qa_bugs" in sql
    )


@pytest.mark.asyncio
async def test_restricted_namespace_reads_the_closed_sources(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PRIV)
    await s.incidents_in_area(
        ORG,
        PRIV,
        component="MergeTree",
        path_prefix="src/Storages/MergeTree/",
        term="logical error",
        days=30,
        untested=True,
    )
    sql, params, cred = up.calls[-1]
    assert cred == "qa" and "FROM qa_intelligence.core_incidents FINAL" in sql
    assert (
        "primary_component = {component:String}" in sql
        and "startsWith(top_function, {prefix:String})" in sql
    )
    assert "ifNull(has_test, 0) = 0" in sql and params["days"] == 30
    assert params["t0"] == ["LOGICAL", "Logical", "logical"] and params["t1"] == [
        "ERROR",
        "Error",
        "error",
    ]
    # a path read skips the crash machinery frames
    assert (
        "top_function NOT IN {noise:Array(String)}" in sql
        and "src/Common/Exception.cpp" in params["noise"]
    )
    await s.customers_in_area(ORG, PRIV, component="S3Queue", days=365)
    sql, params, _ = up.calls[-1]
    assert "GROUP BY org_name" in sql and "countIf(severity_level = 'critical')" in sql
    # escalations carry coarse components upstream: the name is searched as text too
    assert "hasAnyTokens(title, {t0:Array(String)})" in sql and params["t0"][1] == "S3Queue"
    await s.similar_bugs(ORG, PRIV, text="keeper timeout")
    assert up.calls[-1][1]["sources"] == [
        "github_issue",
        "github_issue_private",
        "core_incident",
        "support_case",
    ]
    await s.customer_state(ORG, PRIV, org_name="Acme Corp", days=365)
    sqls = [c[0] for c in up.calls[-3:]]
    assert any(
        "(hasAnyTokens(org_name, {t0:Array(String)}) AND hasAnyTokens(org_name, {t1:Array(String)}))"
        in q
        and "support_cases" in q
        for q in sqls
    )
    assert any("arrayExists(o -> (hasAnyTokens(o, {t0:Array(String)})" in q for q in sqls)
    assert up.calls[-3][1]["t0"] == ["ACME", "Acme", "acme"]
    assert any("cloud_instances" in q for q in sqls) and any("slack_incidents" in q for q in sqls)
    assert all(c[2] == "qa" for c in up.calls)


@pytest.mark.asyncio
async def test_release_blockers_changelog_and_person_shapes(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    assert await s.release_blockers_with_fixes(ORG, PUB, version="26.3") == []
    sql, params, _ = up.calls[-1]
    assert "status = 'Blockers' AND issue_state = 'OPEN'" in sql and params["v"] == "26.3"
    assert len(up.calls) == 1  # no blockers: nothing else is read

    # blockers → derived ids → issue rows and fixes edges side by side → fixing PRs by id
    issue_id = qn.doc_id("github_issue", f"{REPO}#5")
    calls: list[str] = []

    async def _q(sql, params=None, *, credential="qa", **kw):
        calls.append(" ".join(sql.split()))
        if "release_items" in sql:
            return [{"sid": f"{REPO}#5"}]
        if "FROM qa_intelligence.github_issues" in sql:
            return [
                {
                    "source_id": f"{REPO}#5",
                    "url": "u",
                    "title": "t",
                    "severity_level": "critical",
                    "primary_component": "Keeper",
                    "assignees": ["a"],
                }
            ]
        if "FROM qa_intelligence.edges" in sql:
            assert params["ids"] == [issue_id]
            return [{"to_id": issue_id, "from_id": "pr-6"}, {"to_id": issue_id, "from_id": "pr-7"}]
        if "FROM qa_intelligence.github_prs" in sql:
            assert sorted(params["ids"]) == ["pr-6", "pr-7"]
            return [
                {"id": "pr-6", "source_id": f"{REPO}#6", "is_merged": 1, "minor_version": "26.3"},
                {"id": "pr-7", "source_id": f"{REPO}#7", "is_merged": 0, "minor_version": ""},
            ]
        return []

    monkeypatch.setattr(qi, "query_rows", _q)
    src.reset_cache_for_tests()
    rows = await s.release_blockers_with_fixes(ORG, PUB, version="26.3")
    assert rows == [
        {
            "source_id": f"{REPO}#5",
            "url": "u",
            "title": "t",
            "severity": "critical",
            "component": "Keeper",
            "assignees": ["a"],
            "merged_fixes": [f"{REPO}#6"],
            "open_fixes": [f"{REPO}#7"],
            "fix_releases": ["26.3"],
        }
    ]
    assert len(calls) == 4 and "rel_type = 'fixes' AND to_id IN {ids:Array(UUID)}" in calls[2]
    monkeypatch.setattr(qi, "query_rows", up)
    await s.changelog_entries(ORG, PUB, version="26.3", term="Keeper")
    sql, params, _ = up.calls[-1]
    assert "minor_version = {v:String}" in sql and params["t0"] == ["KEEPER", "Keeper", "keeper"]
    assert "hasAnyTokens(changelog_entry, {t0:Array(String)})" in sql
    # the upstream category keys are snake_case
    assert "changelog_category NOT IN ('', 'not_for_changelog', 'ci', 'build')" in sql
    await s.person_activity(ORG, PUB, login="dev-alice", days=7)
    sqls = [c[0] for c in up.calls[-3:]]
    assert any("author = {login:String}" in q and "is_merged = 1" in q for q in sqls)
    assert any("has(reviewers, {login:String})" in q for q in sqls)
    assert any("github_issues FINAL" in q and "author = {login:String}" in q for q in sqls)
    await s.test_gap_components(ORG, PUB, limit=10)
    assert "ORDER BY gap_ratio DESC" in up.calls[-1][0]


# ---- the router -----------------------------------------------------------------


class _Src:
    """A fake TrackerSource: restricted flag, canned rows per reader, optional failure."""

    def __init__(self, *, restricted: bool, down: set[str] | None = None) -> None:
        self.cfg = type("C", (), {"restricted": restricted})()
        self.down = down or set()
        self.calls: list[str] = []

    def _r(self, name, rows):
        async def f(*a, **k):
            self.calls.append(name)
            if name in self.down:
                raise TrackerUnavailable("OPEN")
            return rows

        return f

    def __getattr__(self, name):
        canned = {
            "incidents_in_area": [
                {
                    "source_id": "acme/core-incidents#9",
                    "severity": "critical",
                    "error_type": "LOGICAL_ERROR",
                    "open": True,
                    "created_at": "2026-09-01",
                    "title": "Logical error in loadDataParts",
                    "has_test": False,
                }
            ],
            "escalations_in_area": [],
            "component_risk_latest": {
                "MergeTree": {
                    "component": "MergeTree",
                    "total_bugs": 40,
                    "open_bugs": 3,
                    "gap_ratio": 0.2,
                    "risk_score": 9.0,
                }
            },
            "open_issues_by_component": [
                {
                    "number": 7,
                    "title": "x",
                    "severity_level": "major",
                    "state": "open",
                    "opened_at": datetime(2026, 9, 2),
                }
            ],
            "customers_in_area": [
                {
                    "org_name": "Acme",
                    "cases": 3,
                    "critical": 1,
                    "open": 1,
                    "last_case": "2026-09-01",
                    "examples": ["acme/escalations#1"],
                }
            ],
            "release_blockers_with_fixes": [
                {
                    "source_id": f"{REPO}#5",
                    "severity": "critical",
                    "title": "t",
                    "assignees": ["a"],
                    "merged_fixes": [f"{REPO}#6"],
                    "open_fixes": [],
                    "fix_releases": ["26.3"],
                }
            ],
            "items_by_number": {
                100: {"repo": REPO, "is_pr": 0, "state": "open", "title": "Keeper crash"}
            },
        }
        return self._r(name, canned.get(name, []))


@pytest.fixture
def wired(monkeypatch):
    holder: dict[str, Any] = {}
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: holder["src"])

    def _use(s: _Src) -> _Src:
        holder["src"] = s
        return s

    return _use


@pytest.mark.asyncio
async def test_router_reports_restricted_required_on_a_standard_namespace(wired) -> None:
    s = wired(_Src(restricted=False))
    ans = await router.answer(
        "Core incidents on MergeTree in the last 30 days", org=ORG, tracker_namespace=PUB
    )
    by = {x.template: x for x in ans.sections}
    assert ans.intent == "area_risk" and not ans.restricted
    assert (
        by["area_incidents"].status == "restricted_required"
        and by["area_escalations"].status == "restricted_required"
    )
    assert by["component_risk"].status == "served" and by["area_open_issues"].status == "served"
    assert "incidents_in_area" not in s.calls  # never called on a standard namespace
    d = ans.as_dict()
    assert (
        d["searched"][0].startswith("area_incidents(")
        and "component='MergeTree'" in d["searched"][0]
    )
    lines = router.render_lines(ans)
    assert lines[0].startswith(
        "Core incidents in this area: available through the restricted tracker namespace only"
    )
    assert any(ln.startswith("Component risk snapshot") for ln in lines)


@pytest.mark.asyncio
async def test_router_serves_restricted_and_marks_failures_and_empties(wired) -> None:
    s = wired(_Src(restricted=True, down={"escalations_in_area"}))
    ans = await router.answer(
        "production risks for MergeTree and which customers use it", org=ORG, tracker_namespace=PRIV
    )
    by = {x.template: x for x in ans.sections}
    assert ans.intent == "customers_in_area" and ans.restricted
    assert (
        by["customers_in_area"].status == "served"
        and by["customers_in_area"].rows[0]["org_name"] == "Acme"
    )
    assert (
        by["area_incidents"].status == "served"
        and by["area_escalations"].status == "tracker_unavailable"
    )
    lines = router.render_lines(ans)
    assert any("Acme: 3 case(s), 1 critical" in ln for ln in lines)
    assert any("no regression test" in ln for ln in lines)
    ans = await router.answer(
        "Open 26.3 blockers that already have merged fixes", org=ORG, tracker_namespace=PUB
    )
    line = router.render_lines(ans)[1]
    assert "fixed by ClickHouse/ClickHouse#6 (first release 26.3)" in line and "assignees a" in line
    ans = await router.answer("hello there", org=ORG, tracker_namespace=PUB)
    assert ans.sections == [] and ans.intent == ""
    ans = await router.answer(
        "what is issue #100", org=ORG, tracker_namespace=PUB, explain_only=True
    )
    assert ans.sections[0].status == "planned" and "items_by_number" not in s.calls


def test_op_is_registered_experimental_viewer_read() -> None:
    op = OPERATIONS_BY_NAME["tracker.answer"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    assert "namespace" in op.input_model.model_fields


@pytest.mark.asyncio
async def test_handler_verdicts(monkeypatch, wired) -> None:
    wired(_Src(restricted=False))
    out = await h.handle_tracker_answer(
        TrackerAnswerInput(org=ORG, namespace=PUB, question="what is issue #100")
    )
    assert out.verdict == "answered" and out.sections[0]["rows"][0]["number"] == 100 and out.lines
    out = await h.handle_tracker_answer(
        TrackerAnswerInput(org=ORG, namespace=PUB, question="hello there")
    )
    assert out.verdict == "unknown" and out.searched == []
    out = await h.handle_tracker_answer(
        TrackerAnswerInput(org=ORG, namespace=PUB, question="what is issue #100", explain_only=True)
    )
    assert out.verdict == "planned" and out.lines == []


def test_ask_route_hands_unknown_shapes_to_the_catalogue() -> None:
    assert ask.classify("Changelog entries in 26.3 mentioning Keeper") == ("catalogue", "changelog")
    assert ask.classify("escalations for customer Acme Corp") == ("catalogue", "customer_state")
    assert ask.classify("is 02345_keeper_session flaky?") == ("flaky", "02345_keeper_session")
    assert ask.classify("what did PR #119663 change") == (
        "",
        "",
    )  # the number route owns bare numbers


@pytest.mark.asyncio
async def test_missing_question_is_refused_in_the_handler_after_the_gate() -> None:
    """The "question or anchors" rule lives in the handler, after the binding
    gate, so a refused principal never learns
    the op's argument shape from a model-level 422; the model itself accepts
    the argument-less call."""
    from server.core.errors import ValidationError

    assert TrackerAnswerInput(org=ORG, namespace=PUB).question == ""
    with pytest.raises(ValidationError, match="question or anchors is required"):
        await h.handle_tracker_answer(TrackerAnswerInput(org=ORG, namespace=PUB))
    with pytest.raises(ValidationError):
        await h.handle_tracker_answer(
            TrackerAnswerInput(org=ORG, namespace=PUB, question=" ", anchors={"numbers": []})
        )


@pytest.mark.asyncio
async def test_incident_area_filters_narrow_the_area_instead_of_widening_it(monkeypatch) -> None:
    """The area is any of component / path / text; the noise-frame and
    `untested` filters must hold for every row, so they are AND-ed onto the
    OR group — never OR-ed into it, which would admit every untested incident."""
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PRIV)
    await s.incidents_in_area(
        ORG, PRIV, component="MergeTree", path_prefix="src/Storages/", untested=True
    )
    sql = " ".join(up.calls[-1][0].split())
    where = sql.split(" WHERE ", 1)[1].split(" ORDER BY ", 1)[0]
    assert "OR top_function NOT IN" not in where and "OR ifNull(has_test" not in where
    assert where.endswith(
        ") AND top_function NOT IN {noise:Array(String)} AND ifNull(has_test, 0) = 0"
    )


@pytest.mark.asyncio
async def test_release_blockers_order_by_severity_rank_not_alphabet(monkeypatch) -> None:
    """With equal merged fixes, a critical blocker lists before a major and a
    major before a low — alphabetical order would put "low" before "major"."""
    sev = {"1": "low", "2": "major", "3": "critical", "4": ""}

    async def _q(sql, params=None, *, credential="qa", **kw):
        if "release_items" in sql:
            return [{"sid": f"{REPO}#{n}"} for n in sev]
        if "FROM qa_intelligence.github_issues" in sql:
            return [
                {"source_id": f"{REPO}#{n}", "severity_level": s, "assignees": []}
                for n, s in sev.items()
            ]
        return []

    monkeypatch.setattr(qi, "query_rows", _q)
    src.reset_cache_for_tests()
    rows = await src.TrackerSource(ORG, PUB).release_blockers_with_fixes(ORG, PUB, version="26.3")
    assert [r["severity"] for r in rows] == ["critical", "major", "low", ""]
