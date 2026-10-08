"""An artefact plans its own QA questions — structured anchors
(a PR's components and paths, an issue number, a customer, a version) plan
through the same rules as prose; four more templates (Slack threads,
release exposure, version caps, duplicates) with their readers enforcing
the restricted class; code.symbol_history carries the file's production
incidents on a restricted namespace."""

from __future__ import annotations

from typing import Any

import pytest

import server.code.app.symbol as cs
import server.code.app.symbol_history as sh
import server.identity.app.namespace_classification as ncls
import server.tracker.app.binding as binding
import server.tracker.app.router as router
import server.tracker.domain.namespaces as qn
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.code.api.models_history import CodeSymbolHistoryInput
from server.settings import settings
from server.tracker.api.models import TrackerAnswerInput
from server.tracker.app import source as tsrc
from server.tracker.domain import catalogue as cat
from server.tracker.domain.errors import TrackerUnavailable
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, PUB, PRIV = "clickhouse", "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"
KC = ("MergeTree", "Keeper", "S3Queue")


# ---- planning from anchors ------------------------------------------------------


def test_a_pr_plans_the_area_picture_from_its_components_and_paths() -> None:
    p = cat.plan_from_anchors(
        {
            "components": ["MergeTree"],
            "paths": ["src/Storages/MergeTree/MergeTreeData.cpp"],
            "words": ["risk", "customer"],
            "days": 180,
        },
        known_components=KC,
    )
    assert p.intent == "customers_in_area"
    assert [s.template for s in p.steps] == [
        "customers_in_area",
        "area_incidents",
        "area_escalations",
    ]
    inc = p.steps[1].params
    assert inc["component"] == "MergeTree" and inc["days"] == 180
    assert inc["path_prefix"] == "src/Storages/MergeTree/MergeTreeData.cpp"


def test_anchors_merge_with_prose_and_kinds_are_honoured() -> None:
    p = cat.plan_from_anchors(
        {"numbers": [41001], "number_kind": "escalation"}, known_components=KC
    )
    assert p.intent == "escalation_context" and p.steps[0].params["number"] == 41001
    p = cat.plan_from_anchors(
        {"customers": ["Acme Corp"]}, question="last 90 days", known_components=KC
    )
    assert p.intent == "customer_state" and p.steps[0].params == {
        "org_name": "Acme Corp",
        "days": 90,
        "limit": 10,
    }
    p = cat.plan_from_anchors({"versions": ["26.3"], "words": ["instances"]}, known_components=KC)
    assert p.intent == "version_exposure" and [s.template for s in p.steps] == ["version_exposure"]
    p = cat.plan_from_anchors(
        {"versions": ["26.3"], "words": ["instances", "release"]}, known_components=KC
    )
    assert [s.template for s in p.steps] == ["version_exposure", "version_limits"]
    assert cat.plan_from_anchors({}, known_components=KC).steps == []
    # "question or anchors" is the handler's rule, after the gate
    # (test_tracker_answer proves the raise) — the model accepts both
    TrackerAnswerInput(org=ORG, namespace=PUB)
    TrackerAnswerInput(org=ORG, namespace=PUB, anchors={"components": ["Keeper"]})


def test_new_rules_from_prose() -> None:
    p = cat.plan("which customers are running 26.3 today", known_components=KC)
    assert p.intent == "version_exposure"
    p = cat.plan(
        "Slack threads about 'S3Queue memory leak' in the incident channels", known_components=KC
    )
    assert p.intent == "slack_threads" and p.steps[0].params["term"] == "S3Queue memory leak"
    assert p.steps[0].params["channel"] == "incident"
    p = cat.plan("is issue #12345 a duplicate of an existing issue?", known_components=KC)
    assert p.intent == "duplicates" and p.steps[0].params["number"] == 12345
    # the Slack word is a whole word: a setting name does not trigger it
    assert cat.plan("26.3 changed the default of max_threads", known_components=KC).intent == ""


# ---- readers -------------------------------------------------------------------


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


class _Upstream:
    def __init__(self, rows=None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.rows = rows or []

    async def __call__(self, sql, params=None, *, credential="qa", **kw):
        self.calls.append((" ".join(sql.split()), dict(params or {}), credential))
        return list(self.rows)


@pytest.mark.asyncio
async def test_new_restricted_readers_refuse_standard_and_keep_their_shapes(monkeypatch) -> None:
    up = _Upstream([])
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, PUB)
    for call in (
        lambda: s.slack_threads_matching(ORG, PUB, term="keeper timeout"),
        lambda: s.version_exposure(ORG, PUB, version="26.3"),
        lambda: s.version_limits(ORG, PUB, version="26.3"),
    ):
        with pytest.raises(TrackerUnavailable, match="restricted tracker namespace"):
            await call()
    assert up.calls == []
    # duplicates is public and keyed on the issue's refs
    await s.duplicates_of(ORG, PUB, number=12345)
    sql, params, cred = up.calls[-1]
    assert cred == "qa" and sorted(params["ids"]) == sorted(
        [qn.doc_id("github_issue", f"{REPO}#12345"), qn.doc_id("github_pr", f"{REPO}#12345")]
    )
    assert (
        "rel_type IN ('duplicate_of', 'related_to', 'reproduces')" in sql
        and "is_private = 0" in sql
    )
    assert "from_id IN {ids:Array(UUID)}" in sql and "id IN (SELECT other_id FROM linked)" in sql
    r = src.TrackerSource(ORG, PRIV)
    await r.slack_threads_matching(
        ORG, PRIV, term="S3Queue memory leak", channel="incident", days=30
    )
    sql, params, cred = up.calls[-1]
    assert (
        cred == "qa" and "FROM qa_intelligence.slack_threads FINAL" in sql and "is_bot = 0" in sql
    )
    assert (
        params["t0"] == ["S3QUEUE", "S3Queue", "S3queue", "s3queue"]
        and params["channel"] == "incident"
    )
    assert params["days"] == 30 and "hasAnyTokens(body, {t2:Array(String)})" in sql
    assert await r.slack_threads_matching(ORG, PRIV, term="ab") == []  # nothing to search on
    out = await r.version_exposure(ORG, PRIV, version="26.3")
    sqls = [c[0] for c in up.calls[-3:]]
    # instances carry the minor itself ("26.4"), not X.Y.Z.W
    assert all("clickhouse_version = {v:String}" in q for q in sqls)
    assert up.calls[-1][1]["v"] == "26.3" and out["version"] == "26.3" and out["instances"] == 0
    await r.version_limits(ORG, PRIV, version="26.2")
    sql, params, _ = up.calls[-1]
    assert "has_version_limit = 1" in sql
    # the cap column is mostly empty upstream: a version narrows only where it is set
    assert "(version_limit_max = '' OR startsWith(version_limit_max, {v:String}))" in sql


# ---- router renders the new sections --------------------------------------------


class _Src:
    def __init__(self, restricted: bool) -> None:
        self.cfg = type("C", (), {"restricted": restricted})()

    async def version_exposure(self, org, ns, *, version, limit=20):
        return {
            "version": version,
            "instances": 120,
            "running": 110,
            "orgs": 40,
            "snapshot": "2026-09-15",
            "by_tier": [
                {"tier": "production", "cloud": "aws", "state": "running", "instances": 90}
            ],
            "top_orgs": [{"org_name": "Acme", "instances": 12, "tiers": ["production"]}],
        }

    async def version_limits(self, org, ns, *, version="", limit=20):
        return [
            {
                "reason": "INC-77 pins below 26.3.5",
                "max": "26.3.4",
                "instances": 4,
                "orgs": 2,
                "examples": ["Acme"],
            }
        ]

    async def duplicates_of(self, org, ns, *, number, limit=20):
        return [
            {
                "rel_type": "duplicate_of",
                "direction": "out",
                "source_id": f"{REPO}#100",
                "open": False,
                "state_reason": "duplicate",
                "title": "Keeper crash on restart",
            }
        ]


@pytest.mark.asyncio
async def test_router_runs_anchor_plans_and_renders_new_templates(monkeypatch) -> None:
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Src(restricted=True))
    ans = await router.answer(
        "",
        org=ORG,
        tracker_namespace=PRIV,
        anchors={"versions": ["26.3"], "words": ["instances", "release"]},
    )
    assert ans.intent == "version_exposure" and [s.status for s in ans.sections] == [
        "served",
        "served",
    ]
    lines = router.render_lines(ans)
    assert lines[0].startswith("Release 26.3: 120 instance(s) (110 running) across 40 org(s)")
    assert any("Acme: 12 instance(s), tiers production" in ln for ln in lines)
    assert any(
        "cap 26.3.4: 4 instance(s) in 2 org(s) — INC-77 pins below 26.3.5" in ln for ln in lines
    )
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Src(restricted=False))
    ans = await router.answer(
        "is issue #100 a duplicate of an existing issue?", org=ORG, tracker_namespace=PUB
    )
    assert ans.intent == "duplicates" and ans.sections[0].status == "served"
    assert (
        "duplicate_of out ClickHouse/ClickHouse#100 (closed, duplicate): Keeper crash on restart"
        in router.render_lines(ans)[1]
    )
    ans = await router.answer(
        "", org=ORG, tracker_namespace=PUB, anchors={"versions": ["26.3"], "words": ["instances"]}
    )
    assert ans.sections[0].status == "restricted_required"


# ---- symbol history carries the file's incidents on a restricted namespace -----

PATH = "src/Storages/MergeTree/MergeTreeData.cpp"


class _Repo:
    async def pr_history_for(self, org, ns, *, paths=None, symbol_names=None, limit=5):
        return [
            {
                "pr_number": 900,
                "title": "Fix crash",
                "author": "a",
                "merged_at": "2026-09-01",
                "linked_issues": [],
                "is_revert": False,
            }
        ]

    async def review_comments_for(
        self, org, ns, paths, *, exclude_pr=0, limit=30, humans_only=True
    ):
        return []

    async def items_by_number(self, org, ns, numbers):
        return {}


class _HistSource:
    def __init__(self, restricted: bool) -> None:
        self.cfg = type("C", (), {"restricted": restricted})()
        self.incident_calls: list[dict[str, Any]] = []

    async def prs_facts(self, org, ns, numbers, *, names=None):
        return {}

    async def open_issues_naming(self, org, ns, tokens, *, limit=5):
        return []

    async def edges_from(self, org, ns, refs, rels=None):
        return []

    async def incidents_in_area(self, org, ns, **kw):
        self.incident_calls.append(kw)
        return [
            {
                "source": "core_incident",
                "source_id": "acme/core-incidents#9",
                "title": "Logical error in loadDataParts",
                "severity": "critical",
                "error_type": "LOGICAL_ERROR",
                "top_function": PATH,
                "url": "u/9",
            }
        ]


@pytest.mark.asyncio
async def test_symbol_history_adds_file_incidents_only_when_restricted(monkeypatch) -> None:
    monkeypatch.setattr(sh, "CodeRepo", lambda: _Repo())
    monkeypatch.setattr(sh, "require_code_intel", lambda: None)
    monkeypatch.setattr(sh, "schedule_code_trace", lambda **kw: None)

    async def _resolve(repo, org, ns, *, symbol_id, name):
        return [
            {
                "qualified_name": "DB::MergeTreeData::loadDataParts",
                "path": PATH,
                "start_line": 10,
                "end_line": 20,
                "tier": "scip",
                "symbol_id": "s1",
            }
        ]

    monkeypatch.setattr(cs, "_resolve_symbols", _resolve)

    async def _authorized(org, code_ns):
        return PRIV

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    holder: dict[str, Any] = {"src": _HistSource(restricted=True)}
    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])
    out = await sh.handle_code_symbol_history(
        CodeSymbolHistoryInput(org=ORG, namespace="code-clickhouse", name="x")
    )
    assert holder["src"].incident_calls == [{"path_prefix": PATH, "days": 365, "limit": 5}]
    assert out.open_issues_naming[0] == {
        "source": "core_incident",
        "source_id": "acme/core-incidents#9",
        "title": "Logical error in loadDataParts",
        "severity": "critical",
        "error_type": "LOGICAL_ERROR",
        "top_function": PATH,
        "url": "u/9",
    }
    holder["src"] = _HistSource(restricted=False)
    out = await sh.handle_code_symbol_history(
        CodeSymbolHistoryInput(org=ORG, namespace="code-clickhouse", name="x")
    )
    assert holder["src"].incident_calls == [] and out.open_issues_naming == []
