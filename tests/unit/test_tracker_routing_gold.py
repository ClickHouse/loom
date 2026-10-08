"""Routing gold set: questions a core-DB engineer, a triager,
a release manager or a support engineer would type, each with the intent the
catalogue must choose. Offline — no database. The set is the regression
suite for the rules: a change that drops a case fails here, and a new shape
of question joins the list before its rule does."""

from __future__ import annotations

from server.tracker.app.router import _KNOWN_COMPONENTS as KC
from server.tracker.domain import catalogue as cat

GOLD: list[tuple[str, str]] = [
    # item / numbers
    ("what is PR #119663", "item"),
    ("status of #118935", "item"),
    ("show issue 117001 and PR 117002", "item"),
    ("is #118935 fixed and backported?", "item"),
    ("who is assigned to issue #116690", "item"),
    # escalation / incident context
    ("All Slack threads, issues and PRs related to escalation #41001", "escalation_context"),
    ("escalation 4102 context", "escalation_context"),
    ("what happened with support escalation #4103?", "escalation_context"),
    ("PRs that fix incident INC-1234, and did any ship in a backport to 26.2?", "incident_context"),
    ("core incident #2124 root cause and fixes", "incident_context"),
    ("incident 2083 timeline", "incident_context"),
    # customer state
    (
        "Find all recent escalations for customer Acme Corp. Provide analytics over all channels.",
        "customer_state",
    ),
    ("what is going on with customer Acme", "customer_state"),
    ("Tesla escalations and instances", "customer_state"),
    # release readiness / changelog
    ("Open 26.3 blockers that already have merged fixes, grouped by assignee", "release_readiness"),
    ("is 26.8 ready to release?", "release_readiness"),
    ("release blockers for 26.6", "release_readiness"),
    ("Changelog entries in 26.3 mentioning Keeper", "changelog"),
    ("what changed in 26.7 for Parquet", "changelog"),
    ("26.4 changelog: MergeTree entries", "changelog"),
    # version exposure
    ("which customers run 26.4", "version_exposure"),
    ("how many instances are on 26.3 and which tiers", "version_exposure"),
    ("customers still on 25.8 with version limits", "version_exposure"),
    # person
    ("What did @jane-doe merge, review, or escalate this week?", "person_activity"),
    ("@dev-bob PRs last 30 days", "person_activity"),
    ("what is @dev42 reviewing", "person_activity"),
    # slack
    ("slack threads about S3Queue memory leak in incident channels", "slack_threads"),
    ("what did the oncall channel say about Keeper timeouts", "slack_threads"),
    ("slack discussions mentioning 'parallel replicas' this month", "slack_threads"),
    # duplicates
    ("is #119714 a duplicate of a known issue", "duplicates"),
    ("similar or duplicate issues of 118935", "duplicates"),
    # test gaps
    ("Top 10 components by untested bug ratio this year", "test_gaps"),
    ("which components have the most bugs without tests", "test_gaps"),
    ("test gap ranking", "test_gaps"),
    # flaky
    ("is 02345_keeper_session flaky?", "flaky"),
    ("02871_parallel_replicas_flaky failures", "flaky"),
    ("flaky test 03211_s3queue_ordered", "flaky"),
    # area risk
    ("Core incidents on MergeTree in the last 30 days with no regression test", "area_risk"),
    ("production risk of ParallelReplicas", "area_risk"),
    ("incidents in src/Storages/S3Queue/ last quarter", "area_risk"),
    ("what is risky about Keeper right now", "area_risk"),
    ("MergeTree", "area_risk"),
    # customers in area
    ("production risks for S3Queue and which customers use it", "customers_in_area"),
    ("which customers escalated about Iceberg", "customers_in_area"),
    ("customers affected by Keeper issues", "customers_in_area"),
    # prs touching
    ("PRs last week touching src/Storages/MergeTree/", "prs_touching"),
    ("recent merged PRs in src/Coordination/", "prs_touching"),
    ("what changed under src/Interpreters/ this month", "prs_touching"),
    # similar bugs
    ("Issues mentioning 'S3Queue memory leak' with component or version context", "similar_bugs"),
    ("known issue: Keeper session timeout after upgrade to 26.4", "similar_bugs"),
    ("segfault in MergeTreeData::loadDataParts", "similar_bugs"),
    ("deadlock in RefreshableMV refresh", "similar_bugs"),
    ('"Cannot adjust final" logical error', "similar_bugs"),
    ("is there a bug report about OOM in parallel hash join", "similar_bugs"),
    # severity board
    ("which kinds of bugs are open", "severity_board"),
    ("bug board", "severity_board"),
    ("open bugs by component and severity", "severity_board"),
    ("what kinds of bugs are there in MergeTree", "severity_board"),
    ("severity breakdown of open issues in Keeper", "severity_board"),
    ("issues by severity for ParallelReplicas in the last 90 days", "severity_board"),
    # a cell of the board: a severity beside a component
    ("open medium bugs in MergeTree", "area_risk"),
    ("critical issues in Keeper", "area_risk"),
    ("low severity bugs in Functions", "area_risk"),
    ("medium bugs mentioning 'parallel replicas' in MergeTree", "similar_bugs"),
    # no route
    ("hello there", ""),
    ("how do I write a materialized view", ""),
    ("thanks", ""),
]


def test_every_gold_question_routes_to_its_intent() -> None:
    misses = []
    for q, want in GOLD:
        got = cat.plan(q, known_components=KC).intent
        if got != want:
            misses.append((q, want, got))
    assert not misses, misses


def test_vocabulary_components_filter_and_class_names_are_text() -> None:
    # a CamelCase class is not a `primary_component` value: it anchors the text,
    # never the component filter, and no component-keyed section is planned on it
    p = cat.plan("segfault in MergeTreeData::loadDataParts", known_components=KC)
    assert p.intent == "similar_bugs"
    step = p.steps[0]
    assert step.template == "similar_bugs" and step.params["component"] == ""
    assert "MergeTreeData" in step.params["text"] and "loadDataParts" in step.params["text"]
    assert [s.template for s in p.steps] == ["similar_bugs"]
    # a vocabulary component filters and plans the component-keyed sections
    p = cat.plan("production risk of ParallelReplicas", known_components=KC)
    assert p.intent == "area_risk" and "component_risk" in [s.template for s in p.steps]
    assert p.steps[0].params["component"] == "ParallelReplicas"


def test_named_customer_without_the_word_customer() -> None:
    p = cat.plan("Tesla escalations and instances", known_components=KC)
    assert p.intent == "customer_state" and p.steps[0].params["org_name"] == "Tesla"
    # a vocabulary component beside the same words is an area, not a customer
    p = cat.plan("Keeper escalations and instances", known_components=KC)
    assert p.intent != "customer_state"
    # "customers" is the customers-in-area question, never a named customer
    p = cat.plan("which customers escalated about Iceberg", known_components=KC)
    assert p.intent == "customers_in_area" and p.steps[0].params["term"] == "Iceberg"


def test_partial_similar_bug_rows_say_how_near() -> None:
    from server.tracker.app import router

    line = router._row_line(
        "similar_bugs",
        {
            "source_id": "ClickHouse/ClickHouse#1",
            "severity": "low",
            "state": "open",
            "title": "Keeper session expired",
            "partial": True,
            "matched_tokens": 2,
            "of_tokens": 3,
        },
    )
    assert line.endswith("Keeper session expired — partial match, 2 of 3 terms")


def test_board_questions_carry_the_vocabulary_components_and_no_limit() -> None:
    # The whole board when no component is named …
    p = cat.plan("which kinds of bugs are open", known_components=KC)
    assert p.intent == "severity_board" and [s.template for s in p.steps] == ["severity_board"]
    assert p.steps[0].params == {"components": [], "days": 0}
    # … the vocabulary components named otherwise, with the window when spoken
    p = cat.plan("issues by severity for ParallelReplicas in the last 90 days", known_components=KC)
    assert p.steps[0].params == {"components": ["ParallelReplicas"], "days": 90}
    # a class name is not a component here either
    p = cat.plan("what kinds of bugs are there in MergeTreeData", known_components=KC)
    assert p.intent == "severity_board" and p.steps[0].params["components"] == []
    # the gaps question keeps its route: untested-bug words are not board words
    assert (
        cat.plan("which components have the most untested bugs", known_components=KC).intent
        == "test_gaps"
    )


def test_a_severity_beside_a_component_plans_the_cell_of_the_board() -> None:
    # The severity rides into the open-issue read, the limit is
    # the cell ceiling (every issue the board counted), and the word is not text
    p = cat.plan("open medium bugs in MergeTree", known_components=KC)
    step = next(s for s in p.steps if s.template == "area_open_issues")
    assert step.params == {
        "component": "MergeTree",
        "severities": ["medium"],
        "limit": cat.CELL_LIMIT,
    }
    assert p.anchors.terms == [] and p.anchors.severities == ["medium"]
    assert [s.template for s in p.steps if s.template == "similar_bugs"] == []
    # "low severity" is one severity; the case is the caller's
    p = cat.plan("Low-Severity open issues in Optimizer", known_components=KC)
    assert p.anchors.severities == ["low"]
    assert next(s for s in p.steps if s.template == "area_open_issues").params["severities"] == [
        "low"
    ]
    # with text to search the similar-bugs route is kept, and the cell rides beside it
    p = cat.plan("medium bugs mentioning 'parallel replicas' in MergeTree", known_components=KC)
    assert p.intent == "similar_bugs"
    assert next(s for s in p.steps if s.template == "area_open_issues").params["severities"] == [
        "medium"
    ]
    # without a severity the read is the request's limit and carries no filter
    step = next(
        s
        for s in cat.plan("MergeTree", known_components=KC).steps
        if s.template == "area_open_issues"
    )
    assert step.params == {"component": "MergeTree", "limit": 10}
    # structured anchors (the dashboard's cell read) plan the same step
    p = cat.plan_from_anchors(
        {"components": ["MergeTree"], "severities": ["Medium"], "words": ["risk"]},
        known_components=KC,
    )
    step = next(s for s in p.steps if s.template == "area_open_issues")
    assert step.params == {
        "component": "MergeTree",
        "severities": ["medium"],
        "limit": cat.CELL_LIMIT,
    }
