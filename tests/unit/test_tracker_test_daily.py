"""The per-test daily roll-up and the CI prior read from it:
the worker's plan and merge, the reader's coverage walk and fold back into
the checks-history row, the previous-outcome streak, the static smells, the
prior, the two remote statements' shapes, the loom-side readers, the worker
pass, and the handler taking the roll-up first with the live tail after it."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any, Self

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
import server.tracker.workers.test_daily as w
from server.settings import settings
from server.tracker.api.models import TrackerTestSignalInput, TrackerTestSignalOutput
from server.tracker.app import source as tsrc
from server.tracker.app import test_signal as tsig
from server.tracker.domain import test_daily as td
from server.tracker.domain import test_signal as ts
from server.tracker.domain.errors import TrackerUnavailable
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, NS = "clickhouse", "tracker-clickhouse"
REPO = "ClickHouse/ClickHouse"
D = date(2026, 9, 15)
NOW = datetime(2026, 9, 16, 9, 40)


# --- the plan --------------------------------------------------------------------------


def test_plan_rolls_today_partial_then_missing_days_newest_first() -> None:
    plan = td.plan_days({}, NOW, backfill_days=3, lag_hours=3.0, max_days=10)
    assert [(p.day.isoformat(), p.partial) for p in plan] == [
        ("2026-09-16", True),
        ("2026-09-15", False),
        ("2026-09-14", False),
        ("2026-09-13", False),
    ]
    today = plan[0]
    assert today.since == datetime(2026, 9, 16) and today.until == datetime(2026, 9, 16, 9)
    assert plan[1].since == datetime(2026, 9, 15) and plan[1].until == datetime(2026, 9, 16)


def test_plan_respects_lag_final_days_and_the_per_pass_cap() -> None:
    # yesterday inside the lag: partial up to the current hour; today too
    early = datetime(2026, 9, 16, 1, 20)
    plan = td.plan_days({}, early, backfill_days=2, lag_hours=3.0, max_days=10)
    assert [(p.day.isoformat(), p.partial, p.until) for p in plan] == [
        ("2026-09-16", True, datetime(2026, 9, 16, 1)),
        ("2026-09-15", True, datetime(2026, 9, 16)),  # whole day read, final only after the lag
        ("2026-09-14", False, datetime(2026, 9, 15)),
    ]
    # a final day is never re-rolled; a partial one is re-rolled when there is something new
    rolled = {
        date(2026, 9, 15): (False, datetime(2026, 9, 16)),
        date(2026, 9, 16): (True, datetime(2026, 9, 16, 9)),
        date(2026, 9, 14): (True, datetime(2026, 9, 15)),
    }
    plan = td.plan_days(rolled, NOW, backfill_days=3, lag_hours=3.0, max_days=10)
    assert [(p.day.isoformat(), p.partial) for p in plan] == [
        ("2026-09-14", False),
        ("2026-09-13", False),
    ]
    later = NOW + timedelta(hours=1)
    plan = td.plan_days(rolled, later, backfill_days=1, lag_hours=3.0, max_days=10)
    assert (
        plan[0].day == date(2026, 9, 16)
        and plan[0].partial
        and plan[0].until == datetime(2026, 9, 16, 10)
    )
    # the cap
    assert len(td.plan_days({}, NOW, backfill_days=30, lag_hours=3.0, max_days=6)) == 6
    # just past midnight: today has nothing yet
    assert (
        td.plan_days({}, datetime(2026, 9, 16, 0, 10), backfill_days=0, lag_hours=3.0, max_days=6)
        == []
    )


# --- the merge -------------------------------------------------------------------------


def _fold(
    name: str, runs: int, fails: int, prs: int = 3, groups: int | None = None
) -> dict[str, Any]:
    return {
        "test_name": name,
        "runs": runs,
        "fails": fails,
        "prs": prs,
        "groups": groups if groups is not None else runs,
        "first_run_at": datetime(2026, 9, 15, 0, 5),
    }


def _seq(name: str, **over: Any) -> dict[str, Any]:
    base = {
        "test_name": name,
        "flips": 2,
        "same_commit_flips": 1,
        "branch_fails": 0,
        "last_true_fail": datetime(1970, 1, 1),
        "last_true_fail_head": "",
        "last_fail_at": datetime(2026, 9, 15, 16, 30),
        "fail_hours_all": [datetime(2026, 9, 15, 16), datetime(2026, 9, 15, 11)],
        "fail_hour_prs": [
            (datetime(2026, 9, 15, 16), 11),
            (datetime(2026, 9, 15, 16), 12),
            (datetime(2026, 9, 15, 11), 11),
            (datetime(2026, 9, 15, 16), 0),
        ],
        "per_pr_raw": [(11, 1, 1, 1), (11, 1, 0, 0), (12, 2, 2, 1), (0, 1, 1, 1)],
    }
    base.update(over)
    return base


def test_merge_day_joins_the_fold_and_the_sequences_per_test() -> None:
    stamp = datetime(2026, 9, 16, 3)
    rows = td.merge_day(
        [_fold("a", 600, 3, groups=500), _fold("b", 5, 0)],
        [_seq("a")],
        org=ORG,
        tracker_namespace=NS,
        day=D,
        partial=False,
        rolled_until=datetime(2026, 9, 16),
        version=7,
        rolled_at=stamp,
    )
    a, b = rows
    assert a["test_name"] == "a" and a["runs"] == 600 and a["fails"] == 3 and a["steps"] == 100
    assert a["flips"] == 2 and a["same_commit_flips"] == 1 and a["day"] == D
    assert a["fail_hours"] == [datetime(2026, 9, 15, 11), datetime(2026, 9, 15, 16)]
    assert (datetime(2026, 9, 15, 16), 0) in a["fail_hour_prs"] and len(a["fail_hour_prs"]) == 4
    # per PR head: failures summed, the longest tail kept, last failed if any sequence's last run did
    assert a["fail_prs"] == [11, 12] and a["fail_pr_fails"] == [2, 2]
    assert a["fail_pr_tail"] == [1, 2] and a["fail_pr_last"] == [1, 1]
    assert a["partial"] == 0 and a["rolled_until"] == datetime(2026, 9, 16) and a["version"] == 7
    # a test with no failure carries zero failure fields
    assert b["fails"] == 0 and b["fail_hours"] == [] and b["fail_prs"] == [] and b["steps"] == 0
    from server.tracker.store.daily import TRACKER_TEST_DAILY_COLS

    assert set(a) == set(TRACKER_TEST_DAILY_COLS)
    # groups estimated above runs is clamped: steps never negative
    assert (
        td.merge_day(
            [_fold("c", 10, 0, groups=12)],
            [],
            org=ORG,
            tracker_namespace=NS,
            day=D,
            partial=True,
            rolled_until=stamp,
            version=1,
            rolled_at=stamp,
        )[0]["steps"]
        == 0
    )


def test_merge_day_converts_an_offset_timestamp_to_naive_utc() -> None:
    """A tz-aware upstream value is converted, not stripped: 18:30 at +02:00
    is 16:30 UTC; a missing value is the epoch."""
    plus2 = timezone(timedelta(hours=2))
    (row,) = td.merge_day(
        [_fold("a", 10, 1)],
        [
            _seq(
                "a",
                last_fail_at=datetime(2026, 9, 15, 18, 30, tzinfo=plus2),
                fail_hours_all=[datetime(2026, 9, 15, 13, tzinfo=plus2)],
                fail_hour_prs=[(datetime(2026, 9, 15, 13, tzinfo=plus2), 11)],
                last_true_fail=None,
            )
        ],
        org=ORG,
        tracker_namespace=NS,
        day=D,
        partial=False,
        rolled_until=datetime(2026, 9, 16),
        version=1,
        rolled_at=datetime(2026, 9, 16, 3),
    )
    assert row["last_fail_at"] == datetime(2026, 9, 15, 16, 30)
    assert row["fail_hours"] == [datetime(2026, 9, 15, 11)]
    assert row["fail_hour_prs"] == [(datetime(2026, 9, 15, 11), 11)]
    assert row["last_true_fail"] == td.EPOCH


def test_hour_rows_count_distinct_tests_and_pr_heads_per_hour() -> None:
    stamp = datetime(2026, 9, 16, 3)
    rows = td.merge_day(
        [_fold("a", 10, 3), _fold("b", 10, 1)],
        [
            _seq("a"),
            _seq(
                "b",
                fail_hours_all=[datetime(2026, 9, 15, 16)],
                fail_hour_prs=[(datetime(2026, 9, 15, 16), 40)],
            ),
        ],
        org=ORG,
        tracker_namespace=NS,
        day=D,
        partial=False,
        rolled_until=stamp,
        version=1,
        rolled_at=stamp,
    )
    hours = td.hour_rows(
        rows, org=ORG, tracker_namespace=NS, day=D, partial=False, version=1, created_at=stamp
    )
    by = {r["hour"]: (r["failing_tests"], r["failing_prs"]) for r in hours}
    assert by[datetime(2026, 9, 15, 16)] == (2, 4)  # a on 11, 12, branch head 0; b on 40
    assert by[datetime(2026, 9, 15, 11)] == (1, 1)
    from server.tracker.store.daily import TRACKER_FAIL_HOURS_COLS

    assert set(hours[0]) == set(TRACKER_FAIL_HOURS_COLS) and hours[0]["day"] == D


# --- coverage and the fold back ---------------------------------------------------------


def test_coverage_walk_stops_at_the_first_gap_or_partial_day() -> None:
    since = datetime(2026, 9, 13, 9)
    rolled = {
        date(2026, 9, 13): (False, datetime(2026, 9, 14)),
        date(2026, 9, 14): (False, datetime(2026, 9, 15)),
        date(2026, 9, 15): (False, datetime(2026, 9, 16)),
        date(2026, 9, 16): (True, datetime(2026, 9, 16, 9)),
    }
    cov = td.coverage(rolled, since, NOW)
    assert cov.days == [date(2026, 9, 13), date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
    assert (
        cov.live_since == datetime(2026, 9, 16, 9)
        and cov.partial_day == date(2026, 9, 16)
        and cov.missing == []
    )
    # a gap: live from the gap's day
    del rolled[date(2026, 9, 15)]
    cov = td.coverage(rolled, since, NOW)
    assert cov.days == [date(2026, 9, 13), date(2026, 9, 14)] and cov.missing == [date(2026, 9, 15)]
    assert cov.live_since == datetime(2026, 9, 15) and cov.as_dict()["missing_days"] == [
        "2026-09-15"
    ]
    # nothing rolled: the live checks read, from `since` itself
    cov = td.coverage({}, since, NOW)
    assert cov.days == [] and cov.live_since == since and not cov.as_dict()["available"]
    # every day final through yesterday, today not rolled: live from today's start
    rolled = {
        d: (False, datetime(d.year, d.month, d.day) + timedelta(days=1))
        for d in (date(2026, 9, 13), date(2026, 9, 14), date(2026, 9, 15))
    }
    cov = td.coverage(rolled, since, NOW)
    assert cov.live_since == datetime(2026, 9, 16) and cov.missing == [date(2026, 9, 16)]


def _daily(day: date, **over: Any) -> dict[str, Any]:
    base = {
        "day": day,
        "runs": 100,
        "fails": 2,
        "prs": 10,
        "groups": 80,
        "steps": 20,
        "flips": 1,
        "same_commit_flips": 0,
        "branch_fails": 0,
        "last_true_fail": datetime(1970, 1, 1),
        "last_true_fail_head": "",
        "last_fail_at": datetime(day.year, day.month, day.day, 12),
        "first_run_at": datetime(day.year, day.month, day.day, 1),
        "fail_hours": [datetime(day.year, day.month, day.day, 12)],
        "fail_hour_prs": [(datetime(day.year, day.month, day.day, 12), 11)],
        "fail_prs": [11],
        "fail_pr_fails": [2],
        "fail_pr_tail": [1],
        "fail_pr_last": [1],
        "partial": 0,
        "rolled_until": datetime(day.year, day.month, day.day) + timedelta(days=1),
    }
    base.update(over)
    return base


def test_fold_window_reproduces_the_history_row_shape() -> None:
    rows = [
        _daily(date(2026, 9, 14)),
        _daily(
            date(2026, 9, 15),
            fail_prs=[12],
            same_commit_flips=1,
            branch_fails=1,
            last_true_fail=datetime(2026, 9, 15, 12),
            last_true_fail_head="master",
        ),
    ]
    live = {
        "runs_total": 30,
        "fails_total": 1,
        "prs_total": 3,
        "flips_total": 1,
        "steps_total": 5,
        "rerun_flips_total": 0,
        "branch_fails": 0,
        "last_true_fail": datetime(1970, 1, 1),
        "last_true_fail_head": "",
        "last_fail_at": datetime(2026, 9, 16, 9, 30),
        "first_run_at": datetime(2026, 9, 16, 9, 1),
        "fail_hours_all": [datetime(2026, 9, 16, 9)],
        "fail_hour_prs": [(datetime(2026, 9, 16, 9), 13)],
        "fail_prs": [13],
    }
    row = td.fold_window(rows, live, max_hours=168)
    assert row is not None
    assert row["runs_total"] == 230 and row["fails_total"] == 5 and row["prs_total"] == 23
    assert row["failing_prs"] == 3 and row["flips_total"] == 3 and row["steps_total"] == 45
    assert row["rerun_flips_total"] == 1 and row["branch_fails"] == 1
    assert (
        row["last_true_fail"] == datetime(2026, 9, 15, 12)
        and row["last_true_fail_head"] == "master"
    )
    assert row["last_fail_at"] == datetime(2026, 9, 16, 9, 30) and row["first_run_at"] == datetime(
        2026, 9, 14, 1
    )
    assert row["fail_hours_all"] == [
        datetime(2026, 9, 14, 12),
        datetime(2026, 9, 15, 12),
        datetime(2026, 9, 16, 9),
    ]
    assert (datetime(2026, 9, 16, 9), 13) in row["fail_hour_prs"]
    # the row feeds the signal features unchanged
    feat = ts.features(row)
    assert feat["runs"] == 230 and feat["rerun_flips"] == 1 and feat["failing_prs"] == 3
    assert feat["flip_rate"] == round(3 / 45, 4) and feat["last_true_fail_head"] == "master"
    # nothing ran: None, as the live reader answers
    assert td.fold_window([], None, max_hours=168) is None
    assert (
        td.fold_window([_daily(date(2026, 9, 14), runs=0)], {"runs_total": 0}, max_hours=168)
        is None
    )


# --- streak, smells, prior --------------------------------------------------------------


def test_streak_reads_the_newest_day_the_head_failed_on() -> None:
    rows = [
        _daily(
            date(2026, 9, 14),
            fail_prs=[11, 12],
            fail_pr_fails=[1, 3],
            fail_pr_tail=[1, 3],
            fail_pr_last=[1, 1],
        ),
        _daily(
            date(2026, 9, 15), fail_prs=[11], fail_pr_fails=[2], fail_pr_tail=[0], fail_pr_last=[0]
        ),
    ]
    s = td.streak(rows, None, 11)
    assert s["known"] and s["fail_days"] == 2 and s["last_fail_day"] == "2026-09-15"
    assert s["tail_streak"] == 0 and s["last_outcome"] == "pass" and not s["subsequent_failure"]
    s = td.streak(rows, None, 12)
    assert s["last_fail_day"] == "2026-09-14" and s["tail_streak"] == 3 and s["subsequent_failure"]
    assert s["source"] == "rollup"
    # the live tail wins when it names the head
    live = {
        "fail_prs": [12],
        "fail_pr_fails": [1],
        "fail_pr_tail": [1],
        "fail_pr_last": [1],
        "last_fail_at": datetime(2026, 9, 16, 9, 30),
    }
    s = td.streak(rows, live, 12)
    assert s["source"] == "live" and s["last_fail_day"] == "2026-09-16" and s["fail_days"] == 2
    # a head that never failed the test: unknown, not a pass
    s = td.streak(rows, live, 99)
    assert not s["known"] and s["last_outcome"] == "unknown" and s["fail_days"] == 0
    assert not td.streak(rows, live, 0)["known"]


def test_smell_flags_count_the_categories() -> None:
    sql = "SELECT sleep(1);\nSELECT now(), rand() FROM remote('h', db.t);\nSET max_threads = 4;\n"
    out = td.smell_flags([sql])
    assert out["available"] and out["loc"] == 3
    assert out["flags"] == ["async_wait", "concurrency", "time", "network", "random"]
    assert out["counts"]["async_wait"] == 1 and out["counts"]["network"] == 1
    assert td.smell_flags(["SELECT 1"])["flags"] == [] and not td.smell_flags([])["available"]
    py = "import time\ntime.sleep(2)\nwith ThreadPoolExecutor() as ex: ...\ncluster.start()\n"
    assert set(td.smell_flags([py])["flags"]) == {"async_wait", "concurrency", "network"}


def test_prior_counts_indicators_and_applies_the_subsequent_failure_rule() -> None:
    feat = {"rerun_flips": 1, "flip_rate": 0.0, "failing_prs": 1, "fail_rate": 0.1}
    smells = {"flags": ["async_wait", "time"]}
    churn = {"available": True, "home_prs": 1, "source_prs": 0}
    red = {"known": True, "subsequent_failure": True}
    p = td.prior(feat, ts.FLAKY, flip_rate=0.02, streak_block=red, smells=smells, churn=churn)
    assert p["flake_indicators"] == [
        "history_rerun_flip",
        "smell_async_wait",
        "smell_time",
        "churn_home",
    ]
    assert p["flake_prior"] == "high" and p["next_run_on_pr"] == "uncertain_flaky"
    p = td.prior(
        {"rerun_flips": 0, "flip_rate": 0.0, "failing_prs": 1, "fail_rate": 1.0},
        ts.REGRESSION,
        flip_rate=0.02,
        streak_block=red,
        smells={"flags": []},
        churn={},
    )
    assert p == {"next_run_on_pr": "likely_fail", "flake_indicators": [], "flake_prior": "none"}
    p = td.prior(
        feat,
        ts.REGRESSION,
        flip_rate=0.02,
        streak_block={"known": True, "subsequent_failure": False},
        smells={},
        churn={},
    )
    assert p["next_run_on_pr"] == "likely_pass" and p["flake_prior"] == "low"
    assert (
        td.prior(feat, ts.FLAKY, flip_rate=0.02, streak_block={}, smells={}, churn={})[
            "next_run_on_pr"
        ]
        == "unknown"
    )


def test_remote_clause_is_parsed_and_pushed_down() -> None:
    r = td.parse_remote_clause(
        "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', '')"
    )
    assert (
        r is not None and r.host == "play.clickhouse.com" and r.qualified_table == "default.checks"
    )
    assert r.pushdown("SELECT 1 FROM default.checks") == (
        "remoteSecure('play.clickhouse.com', view(SELECT 1 FROM default.checks), 'play', '')"
    )
    assert td.parse_remote_clause("qa_intelligence.checks") is None


# --- the remote statements -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # ClickHouse's public CI is this deployment's configuration,
    # not loom's default — the fixture configures it the way the deployment does
    monkeypatch.setattr(
        settings,
        "tracker_checks_from",
        "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', '')",
    )
    monkeypatch.setattr(settings, "tracker_checks_repo", "ClickHouse/ClickHouse")
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(f'{{"{REPO}": "{NS}"}}', restricted=""),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


class _Upstream:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.rows = rows or []

    async def __call__(
        self, sql, params=None, *, credential="qa", max_execution_s=None, query_settings=None, **kw
    ):
        self.calls.append(
            (
                " ".join(sql.split()),
                dict(params or {}),
                {"max_execution_s": max_execution_s, "query_settings": query_settings},
            )
        )
        return list(self.rows)


@pytest.mark.asyncio
async def test_day_statements_run_whole_at_the_remote_under_the_daily_budget(monkeypatch) -> None:
    up = _Upstream([{"test_name": "a", "runs": 1, "fails": 0, "prs": 1, "groups": 1}])
    monkeypatch.setattr(qi, "query_rows", up)
    monkeypatch.setattr(settings, "tracker_test_daily_budget_ms", 60000)
    s = src.TrackerSource(ORG, NS)
    d0, d1 = datetime(2026, 9, 15), datetime(2026, 9, 16)
    assert await s.test_day_fold(ORG, NS, since=d0, until=d1) == up.rows
    sql, params, extra = up.calls[-1]
    assert sql.startswith(
        "SELECT * FROM remoteSecure('play.clickhouse.com', view(WITH test_status IN"
    )
    assert (
        "FROM default.checks WHERE check_start_time >= {d0:DateTime} AND check_start_time < {d1:DateTime} GROUP BY test_name), 'play', '')"
        in sql
    )
    assert (
        "uniq((pull_request_number, check_name)) AS groups" in sql
        and "uniqExact(pull_request_number) AS prs" in sql
    )
    assert params == {"d0": d0, "d1": d1, "fail_statuses": list(ts.FAIL_STATUSES)}
    assert extra == {"max_execution_s": 60.0, "query_settings": {"readonly": 2}}
    await s.test_day_sequences(ORG, NS, since=d0, until=d1)
    sql, params, extra = up.calls[-1]
    assert (
        "view(WITH test_status IN {fail_statuses:Array(String)} AS f SELECT test_name, sum(flips) AS flips"
        in sql
    )
    assert (
        "(test_name, pull_request_number, check_name) IN ( SELECT test_name, pull_request_number, check_name FROM default.checks"
        in sql
    )
    assert "toUInt32(if(first_pass_from_end = 0, runs, first_pass_from_end - 1)) AS tail" in sql
    assert "groupArrayIf((pr, fails, tail, last_failed), pr > 0) AS per_pr_raw" in sql
    assert extra["max_execution_s"] == 60.0
    # the view path: a plain subquery over the clause
    monkeypatch.setattr(settings, "tracker_checks_from", "qa_intelligence.checks")
    src.reset_cache_for_tests()
    await s.test_day_fold(ORG, NS, since=d0, until=d1)
    sql = up.calls[-1][0]
    assert (
        sql.startswith("SELECT * FROM (WITH test_status IN")
        and "FROM qa_intelligence.checks WHERE" in sql
    )
    assert "remoteSecure" not in sql


@pytest.mark.asyncio
async def test_live_history_is_bounded_on_both_sides_and_folds_per_pr(monkeypatch) -> None:
    up = _Upstream(
        [
            {
                "runs_total": 12,
                "fails_total": 2,
                "per_pr_raw": [(11, 1, 1, 1), (11, 1, 0, 0), (0, 1, 1, 1)],
            }
        ]
    )
    monkeypatch.setattr(qi, "query_rows", up)
    since, until = datetime(2026, 9, 16, 9), datetime(2026, 9, 16, 10, 40)
    row = await src.TrackerSource(ORG, NS).test_history(
        ORG, NS, "00001_select_1", since=since, until=until
    )
    assert row is not None and row["fail_prs"] == [11] and row["fail_pr_fails"] == [2]
    assert row["fail_pr_tail"] == [1] and row["fail_pr_last"] == [1]
    sql, params, _ = up.calls[-1]
    assert "check_start_time >= {since:DateTime} AND check_start_time < {until:DateTime}" in sql
    assert params["since"] == since and params["until"] == until
    assert "groupArrayIf((pr, fails, tail, last_failed), fails > 0 AND pr > 0) AS per_pr_raw" in sql
    # `until` None: an hour past now
    src.reset_cache_for_tests()
    await src.TrackerSource(ORG, NS).test_history(ORG, NS, "00001_select_1", since=since)
    stop = up.calls[-1][1]["until"]
    assert (
        timedelta(minutes=59) < stop - datetime.now(UTC).replace(tzinfo=None) <= timedelta(hours=1)
    )


def test_socket_timeout_outlives_the_daily_budget(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_test_daily_budget_ms", 60000)
    assert qi.largest_budget_s() == 60.0 and qi.socket_timeout_s(1.5) == 65


# --- the loom-side readers --------------------------------------------------------------


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.rows = rows or []

    async def query_all(self, sql, params=None):
        self.calls.append(("all", " ".join(sql.split()), dict(params or {})))
        return list(self.rows)

    async def query_one(self, sql, params=None):
        self.calls.append(("one", " ".join(sql.split()), dict(params or {})))
        return self.rows[0] if self.rows else None

    async def execute(self, sql, params=None):
        self.calls.append(("exec", sql, dict(params or {})))

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, list(rows)))


@pytest.mark.asyncio
async def test_repo_reads_are_keyed_final_range_reads_and_writes_are_checked() -> None:
    from server.tracker.store.daily import TrackerDailyRepo

    ch = _CH(
        [
            {"day": date(2026, 9, 15), "partial": 0, "rolled_until": datetime(2026, 9, 16)},
            {"day": "2026-09-16", "partial": 1, "rolled_until": datetime(2026, 9, 16, 9)},
        ]
    )
    repo = TrackerDailyRepo(ch)  # type: ignore[arg-type]
    rolled = await repo.rolled_days(ORG, NS, date(2026, 9, 1), date(2026, 9, 16))
    assert rolled == {
        date(2026, 9, 15): (False, datetime(2026, 9, 16)),
        date(2026, 9, 16): (True, datetime(2026, 9, 16, 9)),
    }
    kind, sql, params = ch.calls[-1]
    assert (
        "FROM loom.qa_rollup_days FINAL WHERE org = {org:String} AND qa_namespace = {ns:String} AND day >= {d0:Date} AND day <= {d1:Date}"
        in sql
    )
    await repo.window(ORG, NS, "00001_select_1", date(2026, 9, 1), date(2026, 9, 16))
    sql = ch.calls[-1][1]
    assert (
        "FROM loom.qa_test_daily FINAL WHERE org = {org:String} AND qa_namespace = {ns:String} AND test_name = {t:String} AND day >= {d0:Date} AND day <= {d1:Date} ORDER BY day"
        in sql
    )
    assert "PREWHERE" not in sql
    await repo.hour_stats(ORG, NS, [datetime(2026, 9, 15, 16, 30), datetime(2026, 9, 15, 16, 5)])
    kind, sql, params = ch.calls[-1]
    assert (
        params["hours"] == [datetime(2026, 9, 15, 16)] and "hour IN {hours:Array(DateTime)}" in sql
    )
    assert await repo.hour_stats(ORG, NS, []) == [] and len(ch.calls) == 3
    ch.rows = [{"median_tests": 36, "hours": 700}]
    assert await repo.hour_median(ORG, NS, datetime(2026, 8, 17), datetime(2026, 9, 16)) == 36
    ch.rows = [{"median_tests": 0, "hours": 0}]
    assert await repo.hour_median(ORG, NS, datetime(2026, 8, 17), datetime(2026, 9, 16)) == 0
    # writes carry every column or fail loudly
    with pytest.raises(ValueError, match="tracker_test_daily"):
        await repo.insert_daily([{"org": ORG}])
    await repo.insert_rolled_day(
        {
            "org": ORG,
            "qa_namespace": NS,
            "day": D,
            "partial": 0,
            "rolled_until": datetime(2026, 9, 16),
            "tests": 3,
            "statements": 2,
        }
    )
    table, rows = ch.inserts[-1]
    assert (
        table == "loom.qa_rollup_days"
        and rows[0]["version"] > 0
        and rows[0]["rolled_at"] is not None
    )


# --- the worker pass ---------------------------------------------------------------------


class _WSource:
    def __init__(self, fail_at: int = -1) -> None:
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.fail_at = fail_at

    async def test_day_fold(self, org, ns, *, since, until):
        self.calls.append(("fold", since, until))
        if len(self.calls) == self.fail_at:
            raise TrackerUnavailable("qa_intel[qa] upstream quota: Code: 201")
        return [_fold("a", 10, 2), _fold("b", 4, 0)]

    async def test_day_sequences(self, org, ns, *, since, until):
        self.calls.append(("seq", since, until))
        return [_seq("a")]


class _WRepo:
    def __init__(self, rolled: dict[date, tuple[bool, datetime]] | None = None) -> None:
        self.rolled = rolled or {}
        self.daily: list[dict[str, Any]] = []
        self.hours: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []

    async def rolled_days(self, org, ns, since_day, until_day):
        return dict(self.rolled)

    async def insert_daily(self, rows):
        self.daily.extend(rows)

    async def insert_hours(self, rows):
        self.hours.extend(rows)

    async def insert_rolled_day(self, row):
        self.ledger.append(row)


@pytest.fixture
def worker(monkeypatch):
    repo = _WRepo({date(2026, 9, 14): (False, datetime(2026, 9, 15))})
    source = _WSource()
    monkeypatch.setattr(w, "_repo", lambda: repo)
    monkeypatch.setattr(w, "_source", lambda org, ns: source)
    monkeypatch.setattr(w, "_namespaces", lambda: [(ORG, NS)])
    monkeypatch.setattr(
        "server.tracker.domain.sources.credential_configured", lambda credential="qa": True
    )
    monkeypatch.setattr(settings, "tracker_test_daily_backfill_days", 3)
    monkeypatch.setattr(settings, "tracker_test_daily_pace_s", 0.0)
    monkeypatch.setattr(settings, "tracker_test_daily_max_days_per_pass", 6)
    monkeypatch.setattr(settings, "tracker_test_daily_lag_hours", 3.0)

    class _Now(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW.replace(tzinfo=tz) if tz else NOW

    monkeypatch.setattr(w, "datetime", _Now)
    return repo, source


@pytest.mark.asyncio
async def test_worker_pass_rolls_the_planned_days_and_writes_the_ledger_last(worker) -> None:
    repo, source = worker
    result = await w.run_pass()
    assert result.namespaces == 1 and result.days_rolled == 3 and result.statements == 6
    assert result.stopped == "" and result.errors == []
    # today partial to the hour, then 09-15 final, 09-13 final; 09-14 is settled and skipped
    assert [(c[0], c[1].date().isoformat(), c[2]) for c in source.calls][:2] == [
        ("fold", "2026-09-16", datetime(2026, 9, 16, 9)),
        ("seq", "2026-09-16", datetime(2026, 9, 16, 9)),
    ]
    assert [r["day"].isoformat() for r in repo.ledger] == ["2026-09-16", "2026-09-15", "2026-09-13"]
    assert repo.ledger[0]["partial"] == 1 and repo.ledger[1]["partial"] == 0
    assert (
        repo.ledger[0]["tests"] == 2
        and repo.ledger[0]["failing_tests"] == 1
        and repo.ledger[0]["statements"] == 2
    )
    assert len(repo.daily) == 6 and len(repo.hours) == 6  # 2 tests × 3 days; a's 2 hours × 3 days
    assert {r["partial"] for r in repo.daily if r["day"] == date(2026, 9, 16)} == {1}
    # The pass states its counts first — days planned / rolled, every row written
    # (6 test rows + 6 failing hours + 3 ledger rows)
    assert result.days_planned == 3 and result.hour_rows == 6
    from server.workers.status import get_registry

    outcome = get_registry().snapshot()["tracker_test_daily"]["last_outcome"]
    assert list(outcome)[:3] == ["items_planned", "items_done", "rows_written"]
    assert (outcome["items_planned"], outcome["items_done"], outcome["rows_written"]) == (3, 3, 15)
    assert repo.ledger[0]["source"].startswith("remoteSecure('play.clickhouse.com'")


@pytest.mark.asyncio
async def test_the_ledger_stores_the_checks_source_without_its_password(
    worker, monkeypatch
) -> None:
    repo, _ = worker
    monkeypatch.setattr(
        settings,
        "tracker_checks_from",
        "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', 'pw-secret')",
    )
    await w.run_pass()
    assert {r["source"] for r in repo.ledger} == {
        "remoteSecure('play.clickhouse.com', 'default', 'checks', ...)"
    }


@pytest.mark.asyncio
async def test_worker_stops_at_the_quota_and_records_it_without_failing(worker) -> None:
    repo, source = worker
    source.fail_at = 3  # the second day's fold statement is refused
    result = await w.run_pass()
    assert result.days_rolled == 1 and "upstream quota" in result.stopped and result.errors == []
    assert len(repo.ledger) == 1  # a half-read day writes nothing
    from server.workers.status import get_registry

    outcome = get_registry().snapshot()["tracker_test_daily"]["last_outcome"]
    assert outcome["days_rolled"] == 1


@pytest.mark.asyncio
async def test_worker_is_a_no_op_without_the_credential(worker, monkeypatch) -> None:
    repo, source = worker
    monkeypatch.setattr(
        "server.tracker.domain.sources.credential_configured", lambda credential="qa": False
    )
    result = await w.run_pass()
    assert result.namespaces == 0 and "not configured" in result.stopped and source.calls == []


def test_worker_is_registered_and_described() -> None:
    from server.workers.registry import enabled_workers, worker_meta

    assert (
        "tracker_test_daily" in worker_meta()
        and worker_meta()["tracker_test_daily"]["interval_seconds"] == 1800
    )
    spec = {s.name: s for s in enabled_workers()}.get("tracker_test_daily")
    if settings.tracker_test_daily_enabled:
        assert spec is not None and spec.deadline_seconds() == 900.0


# --- the handler: roll-up first, live tail after ---------------------------------------------


class _Source:
    def __init__(
        self,
        live: dict[str, Any] | None,
        hours: list[dict[str, Any]] | None = None,
        fail_live: bool = False,
    ) -> None:
        self.live, self.hours, self.fail_live = live, hours or [], fail_live
        self.calls: list[Any] = []

    async def test_history(self, org, ns, name, *, since, until=None):
        self.calls.append(("history", since, until))
        if self.fail_live:
            raise TrackerUnavailable("test_history exceeded 8000 ms")
        return self.live

    async def co_failures_in_hours(self, org, ns, name, hours):
        self.calls.append(("co", hours))
        return self.hours

    async def failing_tests_per_hour_median(self, org, ns, *, since):
        self.calls.append(("baseline", since))
        return 36


class _Daily:
    def __init__(
        self,
        rolled: dict[date, tuple[bool, datetime]],
        rows: list[dict[str, Any]],
        hours: list[dict[str, Any]],
        median: int = 40,
    ) -> None:
        self.rolled, self.rows, self.hours, self.median = rolled, rows, hours, median
        self.calls: list[Any] = []

    async def rolled_days(self, org, ns, since_day, until_day):
        self.calls.append(("rolled", since_day, until_day))
        return dict(self.rolled)

    async def window(self, org, ns, name, since_day, until_day):
        self.calls.append(("window", name, since_day, until_day))
        return [r for r in self.rows if since_day <= r["day"] <= until_day]

    async def hour_stats(self, org, ns, hours):
        self.calls.append(("hours", hours))
        return [r for r in self.hours if r["hour"] in set(hours)]

    async def hour_median(self, org, ns, since, until):
        self.calls.append(("median", since, until))
        return self.median


class _Repo:
    async def paths_by_stem(self, org, ns, stems):
        return [{"path": "tests/queries/0_stateless/02345_keeper_session.sql"}]

    async def sources_of_tests(self, org, ns, test_paths):
        return ["src/Coordination/KeeperServer.cpp"]

    async def pr_churn_touching(self, org, ns, *, paths, prefix="", since, limit=50):
        return [{"pr_number": 9, "title": "t", "merged_at": datetime(2026, 9, 10)}]

    async def chunks_for_paths(self, org, ns, model, paths, *, per_path=2):
        self.chunk_call = (model, list(paths), per_path)
        return [{"path": paths[0], "content": "SELECT sleep(1);\nSELECT now();\n"}]


@pytest.fixture
def wired(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(tsig, "_code_repo", lambda: repo)
    monkeypatch.setattr(settings, "code_org", ORG)
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")

    class _E:
        model_name = "text-embedding-3-large"

    import server.auth.authz as authz
    import server.embedding.embedder as emb

    monkeypatch.setattr(emb, "get_embedder", lambda: _E())

    async def _ok(org, ns, role=None, *, hides_answer=True):
        return True

    monkeypatch.setattr(authz, "row_in_scope", _ok)
    return repo


def _pin_clock(monkeypatch: pytest.MonkeyPatch, at: datetime) -> datetime:
    """Fix the handler's clock: the roll-up fixtures are only valid off the midnight hour."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.combine(at.date(), at.time(), tzinfo=tz)

    monkeypatch.setattr(tsig, "datetime", _Clock)
    return at


def _window_days(now: datetime, n: int = 14) -> dict[date, tuple[bool, datetime]]:
    out: dict[date, tuple[bool, datetime]] = {}
    for back in range(1, n + 2):
        d = (now - timedelta(days=back)).date()
        out[d] = (False, datetime(d.year, d.month, d.day) + timedelta(days=1))
    out[now.date()] = (True, now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1))
    return out


@pytest.mark.asyncio
async def test_handler_reads_the_rollup_then_the_live_tail(monkeypatch, wired) -> None:
    now = _pin_clock(monkeypatch, datetime.now(UTC).replace(tzinfo=None, hour=12, minute=30))
    hour = now.replace(minute=0, second=0, microsecond=0)
    yday = (now - timedelta(days=1)).date()
    storm_hour = datetime(yday.year, yday.month, yday.day, 16)
    rows = [
        _daily(
            yday,
            fail_prs=[11],
            fail_pr_tail=[2],
            fail_pr_last=[1],
            fail_hours=[storm_hour],
            fail_hour_prs=[(storm_hour, 11)],
            same_commit_flips=0,
            flips=0,
        )
    ]
    daily = _Daily(
        _window_days(now), rows, [{"hour": storm_hour, "failing_tests": 7414, "failing_prs": 26}]
    )
    live = {
        "runs_total": 20,
        "fails_total": 1,
        "prs_total": 2,
        "flips_total": 0,
        "steps_total": 3,
        "rerun_flips_total": 0,
        "branch_fails": 0,
        "fail_hours_all": [hour],
        "fail_hour_prs": [(hour, 11)],
        "fail_prs": [11],
        "fail_pr_fails": [1],
        "fail_pr_tail": [1],
        "fail_pr_last": [1],
        "last_fail_at": now,
        "first_run_at": hour,
        "last_true_fail": datetime(1970, 1, 1),
    }
    s = _Source(live, hours=[{"h": hour, "other_tests": 3, "other_prs": 2}])
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    monkeypatch.setattr(tsig, "_daily_repo", lambda: daily)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(
            org=ORG, namespace=NS, test_name="02345_keeper_session", window_days=14, pr_number=11
        )
    )
    assert isinstance(out, TrackerTestSignalOutput) and not out.tracker_unavailable
    # the roll-up covered every day through the partial one; the live read starts at its rolled_until
    assert (
        out.rollup["available"]
        and out.rollup["days"] == 15
        and out.rollup["partial_day"] == now.date().isoformat()
    )
    live_since = datetime.fromisoformat(out.rollup["live_since"])
    assert live_since == hour - timedelta(hours=1)
    assert s.calls[0][0] == "history" and s.calls[0][1] == live_since and s.calls[0][2] > now
    # the fold: roll-up + live
    assert (
        out.features["runs"] == 120
        and out.features["fails"] == 3
        and out.features["failing_prs"] == 1
    )
    assert (
        out.features["prs"] == 12 and out.features["prs_kind"] == "head_days"
    )  # 10 (day) + 2 (live)
    assert out.features["fail_hours"] == [storm_hour.isoformat(), hour.isoformat()]
    # the storm leg: K from the roll-up median (5 × 40 = 200), the rolled hour from the hour table,
    # the live hour from the live co-failure read; the 09-15 16:00 storm fires the storm rule
    assert out.infra["k"] == 200 and out.infra["baseline_source"] == "rollup"
    assert (
        out.infra["rollup_hours"] == 1
        and out.infra["live_hours"] == 1
        and out.infra["storm_hours"] == 1
    )
    assert ("hours", [storm_hour]) in daily.calls and ("co", [hour]) in s.calls
    assert out.verdict == ts.INFRA and out.infra["share"] == 0.5
    # streak: the live tail names the head
    assert out.streak["known"] and out.streak["source"] == "live" and out.streak["tail_streak"] == 1
    assert out.streak["subsequent_failure"] and out.prior["next_run_on_pr"] == "likely_fail"
    # smells from the home file's chunks, read with the embedder's label
    assert out.smells["available"] and out.smells["flags"] == ["async_wait", "time"]
    assert wired.chunk_call == (
        "text-embedding-3-large",
        ["tests/queries/0_stateless/02345_keeper_session.sql"],
        6,
    )
    assert (
        "smell_async_wait" in out.prior["flake_indicators"]
        and "churn_home" in out.prior["flake_indicators"]
    )
    assert set(out.timings_ms) == {"history", "co_failures", "churn", "smells"}
    assert set(out.model_dump()) >= {"rollup", "streak", "smells", "prior"}


@pytest.mark.asyncio
async def test_handler_survives_a_failed_live_tail_when_the_rollup_holds_rows(
    monkeypatch, wired
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    yday = (now - timedelta(days=1)).date()
    rows = [_daily(yday, same_commit_flips=1)]
    daily = _Daily(_window_days(now), rows, [])
    s = _Source(None, fail_live=True)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    monkeypatch.setattr(tsig, "_daily_repo", lambda: daily)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(
            org=ORG,
            namespace=NS,
            test_name="02345_keeper_session",
            window_days=14,
            include_smells=False,
        )
    )
    assert (
        not out.tracker_unavailable
        and out.verdict == ts.FLAKY
        and "8000 ms" in out.rollup["live_unavailable"]
    )
    assert out.smells == {} and out.streak == {} and "smells" not in out.timings_ms
    # no roll-up and no live: unavailable, with the roll-up block saying why
    monkeypatch.setattr(tsig, "_daily_repo", lambda: _Daily({}, [], []))
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_x")
    )
    assert out.tracker_unavailable and out.rollup["reason"] == "no day of the window is rolled"


@pytest.mark.asyncio
async def test_rollup_switch_off_and_a_failed_rollup_read_take_the_live_path(
    monkeypatch, wired
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    live = {
        "runs_total": 192,
        "fails_total": 17,
        "prs_total": 1,
        "failing_prs": 1,
        "flips_total": 2,
        "steps_total": 183,
        "rerun_flips_total": 2,
        "fail_hours_all": [],
        "fail_hour_prs": [],
    }
    s = _Source(live)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)

    class _Boom:
        async def rolled_days(self, *a, **k):
            raise RuntimeError("no such table")

    monkeypatch.setattr(tsig, "_daily_repo", lambda: _Boom())
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_x", window_days=7)
    )
    assert out.verdict == ts.FLAKY and out.rollup == {
        "available": False,
        "reason": "roll-up read failed: RuntimeError",
    }
    since = s.calls[0][1]
    assert timedelta(days=7) - timedelta(seconds=5) <= now - since <= timedelta(days=7, hours=1)


def test_op_fields_settings_and_descriptions_cite_the_decision() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    op = OPERATIONS_BY_NAME["tracker.test_signal"]
    fields = op.input_model.model_fields
    assert fields["pr_number"].default == 0 and fields["include_smells"].default is True
    out_fields = op.output_model.model_fields
    assert {"rollup", "streak", "smells", "prior"} <= set(out_fields)
    assert (
        "`streak`" in TOOL_DESCRIPTIONS["tracker.test_signal"]
        and "roll-up" in TOOL_DESCRIPTIONS["tracker.test_signal"]
    )
    assert (
        settings.tracker_test_daily_interval_seconds == 1800
        and settings.tracker_test_daily_backfill_days == 30
    )
    assert (
        settings.tracker_test_daily_max_days_per_pass == 6
        and settings.tracker_test_daily_lag_hours == 3.0
    )
    assert settings.tracker_test_daily_budget_ms == 60000
    assert settings.tracker_test_signal_smell_chunks == 6
