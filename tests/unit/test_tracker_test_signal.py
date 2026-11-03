"""tracker.test_signal: a failing test is read against its own
CI history — flip rate, entropy, rerun flips, branch-head failures, the
co-failure hours and the ledger churn — before anyone blames the change.
The verdict's thresholds are settings and echoed; `no_signal` is silence;
a source that cannot answer is `tracker_unavailable`; tracker.flaky's payload is byte
for byte the plain flaky shape unless `include_signal` is asked for."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.api.models import (
    TrackerFlakyInput,
    TrackerTestSignalInput,
    TrackerTestSignalOutput,
)
from server.tracker.app import handlers as h
from server.tracker.app import source as tsrc
from server.tracker.app import test_signal as tsig
from server.tracker.domain import test_signal as ts
from server.tracker.domain.errors import TrackerUnavailable
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, NS = "clickhouse", "tracker-clickhouse"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"
PRIV = "tracker-example-private"


# --- pure features and verdicts -------------------------------------------------


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "runs_total": 192,
        "fails_total": 17,
        "prs_total": 1,
        "failing_prs": 1,
        "branch_fails": 0,
        "last_true_fail": datetime(1970, 1, 1),
        "last_true_fail_head": "",
        "last_fail_at": datetime(2026, 9, 16, 3, 44, 40),
        "first_run_at": datetime(2026, 9, 16, 1),
        "flips_total": 2,
        "steps_total": 183,
        "rerun_flips_total": 2,
        "fail_hours_all": [datetime(2026, 9, 16, 3)],
    }
    base.update(over)
    return base


def test_features_from_the_aggregated_row() -> None:
    f = ts.features(_row())
    assert f["runs"] == 192 and f["fails"] == 17 and f["fail_rate"] == 0.0885
    assert f["flip_rate"] == round(2 / 183, 4) and f["rerun_flips"] == 2
    assert (
        f["entropy"] == ts.binary_entropy(0.0885) and 0.4 < f["entropy"] < 0.5
    )  # of the rounded rate
    assert f["last_true_fail"] is None and f["last_true_fail_head"] is None  # epoch = none
    assert f["fail_hours"] == ["2026-09-16T03:00:00"]
    assert ts.features(None)["runs"] == 0 and ts.features({})["fail_hours"] == []


def test_wilson_lower_bound_is_small_for_small_n() -> None:
    assert ts.wilson_lower(0, 0) == 0.0
    assert ts.wilson_lower(1, 5) < 0.05 < ts.wilson_lower(17, 192) < 17 / 192
    assert ts.wilson_lower(50, 50) > 0.9


def test_verdict_rules_in_order() -> None:
    th = ts.Thresholds()
    # too few runs, or no failure: silence
    assert ts.verdict(ts.features(_row(runs_total=3, fails_total=1)), 0, th)[0] == ts.NO_SIGNAL
    assert (
        ts.verdict(ts.features(_row(fails_total=0, rerun_flips_total=0)), 0, th)[0] == ts.NO_SIGNAL
    )
    # a rerun of the same commit passed: flaky, whatever else
    v, why, lb = ts.verdict(ts.features(_row()), 1, th)
    assert v == ts.FLAKY and "rerun" in why[0] and lb == ts.wilson_lower(17, 192)
    # no rerun flip; every failing hour was a storm hour: infra
    storm = _row(
        rerun_flips_total=0,
        flips_total=1,
        fails_total=2,
        fail_hours_all=[datetime(2026, 9, 11, 10), datetime(2026, 9, 12, 10)],
    )
    assert ts.verdict(ts.features(storm), 2, th)[0] == ts.INFRA
    assert ts.verdict(ts.features(storm), 1, th)[0] == ts.INFRA  # share 0.5 meets the threshold
    # flips often, no storm: flaky by flip rate
    flippy = _row(
        rerun_flips_total=0, flips_total=10, steps_total=100, fails_total=5, failing_prs=1
    )
    v, why, _ = ts.verdict(ts.features(flippy), 0, th)
    assert v == ts.FLAKY and "flip rate 0.1" in why[0]
    # several PR heads at a low fail rate: flaky
    spread = _row(rerun_flips_total=0, flips_total=1, fails_total=3, failing_prs=3, prs_total=900)
    assert ts.verdict(ts.features(spread), 0, th)[0] == ts.FLAKY
    # one PR head, no rerun pass, no storm, never on a branch head: regression candidate
    one = _row(rerun_flips_total=0, flips_total=1, fails_total=17, failing_prs=1)
    v, why, _ = ts.verdict(ts.features(one), 0, th)
    assert v == ts.REGRESSION and "never failed on a branch head" in why[1]
    # a failure on master is named as a true failure
    master = _row(
        rerun_flips_total=0,
        flips_total=1,
        branch_fails=2,
        last_true_fail=datetime(2026, 9, 15, 12),
        last_true_fail_head="master",
    )
    v, why, _ = ts.verdict(ts.features(master), 0, th)
    assert v == ts.REGRESSION and "branch head master" in why[1]


def test_storm_k_is_an_outlier_of_the_window_not_a_busy_hour() -> None:
    th = ts.Thresholds()
    assert th.storm_k(36) == 180 and th.storm_k(10) == 100 and th.storm_k(0) == 100  # the floor
    assert ts.Thresholds(cofail_multiple=10.0).storm_k(36) == 360


def test_same_test_on_three_prs_in_one_hour_is_a_shared_cause() -> None:
    th = ts.Thresholds()
    row = _row(
        rerun_flips_total=0,
        flips_total=1,
        fails_total=6,
        failing_prs=3,
        prs_total=900,
        fail_hour_prs=[
            (datetime(2026, 9, 15, 16), 11),
            (datetime(2026, 9, 15, 16), 12),
            (datetime(2026, 9, 15, 16), 13),
            (datetime(2026, 9, 15, 16), 0),
            (datetime(2026, 9, 14, 9), 11),
        ],
        fail_hours_all=[datetime(2026, 9, 14, 9), datetime(2026, 9, 15, 16)],
    )
    feat = ts.features(row)
    # branch heads (pr = 0) do not count as a PR; the 09-14 hour has one PR and is not listed
    assert feat["fail_hour_prs"] == {"2026-09-15T16:00:00": 3}
    assert ts.multi_pr_hours(feat, 3) == ["2026-09-15T16:00:00"]
    v, why, _ = ts.verdict(feat, 0, th)
    assert v == ts.INFRA and "3 different PR heads within one hour" in why[0]
    # the same failures spread over hours with one PR each: the flaky spread rule instead
    row["fail_hour_prs"] = [
        (datetime(2026, 9, 15, 16), 11),
        (datetime(2026, 9, 14, 9), 12),
        (datetime(2026, 9, 13, 9), 13),
    ]
    assert ts.verdict(ts.features(row), 0, th)[0] == ts.FLAKY


def test_co_fail_hours_need_k_tests_and_p_prs() -> None:
    rows = [
        {"h": datetime(2026, 9, 11, 10), "other_tests": 726, "other_prs": 37},
        {"h": datetime(2026, 9, 12, 10), "other_tests": 45, "other_prs": 12},
        {"h": datetime(2026, 9, 13, 10), "other_tests": 152, "other_prs": 2},  # one PR's own storm
    ]
    n, hours = ts.co_fail_hours(rows, k=20, min_prs=3)
    assert n == 2 and hours == ["2026-09-11T10:00:00", "2026-09-12T10:00:00"]
    assert ts.co_fail_hours(rows, k=1000, min_prs=3) == (0, [])


def test_home_convention() -> None:
    assert ts.home_of("02345_keeper_session") == (["02345_keeper_session"], "")
    assert ts.home_of("02345_keeper_session.sql") == (["02345_keeper_session"], "")
    assert ts.home_of("test_keeper_multinode/test.py::test_read_write[param-1]") == (
        [],
        "tests/integration/test_keeper_multinode/",
    )
    assert ts.home_of("test_backup_restore") == ([], "tests/integration/test_backup_restore/")
    assert ts.home_of("Build ClickHouse") == ([], "") and ts.home_of("Suite.Case") == ([], "")
    assert ts.is_test_home_path("tests/queries/0_stateless/00001_select_1.sql")
    assert not ts.is_test_home_path("tests/queries/0_stateless/00001_select_1.reference")


# --- the source readers ---------------------------------------------------------


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
        tracker_sources_json(f'{{"{REPO}": "{NS}", "{PREPO}": "{PRIV}"}}', restricted=PRIV),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    monkeypatch.setattr(settings, "tracker_live_checks_budget_ms", 8000)
    # No day is rolled here, so the op takes the live path
    monkeypatch.setattr(tsig, "_daily_repo", lambda: _NoRollup())
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


class _NoRollup:
    async def rolled_days(self, org, ns, since_day, until_day):
        return {}

    async def window(self, org, ns, name, since_day, until_day):
        return []


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
async def test_history_read_is_keyed_by_test_name_and_window_and_reads_as_readonly_2(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "tracker_checks_from", "qa_intelligence.checks")
    up = _Upstream([_row()])
    monkeypatch.setattr(qi, "query_rows", up)
    since = datetime(2026, 8, 17, 4, 0, 0)
    row = await src.TrackerSource(ORG, NS).test_history(
        ORG, NS, " 05218_allow_executable_tables ", since=since
    )
    assert row is not None and row["runs_total"] == 192
    sql, params, extra = up.calls[-1]
    assert (
        "FROM qa_intelligence.checks WHERE test_name = {t:String} AND check_start_time >= {since:DateTime} AND check_start_time < {until:DateTime}"
        in sql
    )
    assert params["t"] == "05218_allow_executable_tables" and params["since"] == since
    assert params["fail_statuses"] == list(ts.FAIL_STATUSES)
    # the table function behind the view is refused under readonly = 1; the read says 2
    assert extra["query_settings"] == {"readonly": 2} and extra["max_execution_s"] == 8.0
    # no privacy predicate: the table has no is_private column (public CI)
    assert "is_private" not in sql
    # grouped per (PR, check) with the commit-level rerun intersection and the time-ordered flips
    assert "GROUP BY pr, chk" in sql and "arrayIntersect(groupUniqArrayIf(commit_sha, f)" in sql
    assert "arrayPopBack(seq), arrayPopFront(seq)" in sql and "-168" in sql
    # an empty window is None, not a row of zeros
    up.rows = [{"runs_total": 0}]
    src.reset_cache_for_tests()
    assert (
        await src.TrackerSource(ORG, NS).test_history(ORG, NS, "x_never_ran", since=since) is None
    )


@pytest.mark.asyncio
async def test_checks_source_clause_is_a_setting(monkeypatch) -> None:
    up = _Upstream([_row()])
    monkeypatch.setattr(qi, "query_rows", up)
    await src.TrackerSource(ORG, NS).test_history(
        ORG, NS, "00001_select_1", since=datetime(2026, 8, 1)
    )
    # the default names the table function directly: the view's filter is applied on the QA
    # side and a hot test hits the remote's result cap
    assert (
        "FROM remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', '')"
        in up.calls[-1][0]
    )


@pytest.mark.asyncio
async def test_co_failures_read_names_the_hours_and_excludes_the_test(monkeypatch) -> None:
    up = _Upstream([{"h": datetime(2026, 9, 11, 10), "other_tests": 726, "other_prs": 37}])
    monkeypatch.setattr(qi, "query_rows", up)
    hours = [
        datetime(2026, 9, 11, 10, 47, 3),
        datetime(2026, 9, 11, 10, 5),
        datetime(2026, 9, 12, 10),
    ]
    rows = await src.TrackerSource(ORG, NS).co_failures_in_hours(ORG, NS, "00001_select_1", hours)
    assert rows[0]["other_tests"] == 726
    sql, params, extra = up.calls[-1]
    assert params["hours"] == [
        datetime(2026, 9, 11, 10),
        datetime(2026, 9, 12, 10),
    ]  # hour-truncated, unique
    assert "toStartOfHour(check_start_time) IN {hours:Array(DateTime)}" in sql
    assert "test_name != {t:String}" in sql and params["t"] == "00001_select_1"
    assert extra["query_settings"] == {"readonly": 2}
    assert await src.TrackerSource(ORG, NS).co_failures_in_hours(ORG, NS, "x", []) == []


@pytest.mark.asyncio
async def test_a_namespace_without_the_public_repo_has_no_checks(monkeypatch) -> None:
    up = _Upstream([_row()])
    monkeypatch.setattr(qi, "query_rows", up)

    async def _restricted(org, ns):
        return True

    import server.identity.app.namespace_classification as ncls

    monkeypatch.setattr(ncls, "is_restricted", _restricted)
    with pytest.raises(TrackerUnavailable, match="that repository's CI only"):
        await src.TrackerSource(ORG, PRIV).test_history(
            ORG, PRIV, "00001_select_1", since=datetime(2026, 8, 1)
        )
    assert up.calls == []


@pytest.mark.asyncio
async def test_result_cap_error_is_not_an_outage_on_the_breaker(monkeypatch) -> None:
    class _Client:
        def __init__(self) -> None:
            self.boom = "Code: 396. DB::Exception: Limit for result exceeded, max bytes: 953.67 MiB (TOO_MANY_ROWS_OR_BYTES)"

        async def query(self, sql, parameters=None, settings=None):
            self.last_settings = settings
            raise RuntimeError(self.boom)

    client = _Client()

    async def _get(credential=qi.CREDENTIAL):
        return client

    monkeypatch.setattr(qi, "_get_client", _get)
    for _ in range(4):
        with pytest.raises(RuntimeError):
            await qi.query_rows("SELECT 1", query_settings={"readonly": 2}, max_execution_s=8.0)
    assert qi.circuit_status("qa") == {"state": "CLOSED", "consecutive_failures": 0}
    assert client.last_settings == {"readonly": 2, "max_execution_time": 8.0}
    # the remote's per-user quota (play: 100 queries of one shape per hour) is a rate cap on
    # this statement, not an outage of the QA service
    client.boom = "Code: 201. DB::Exception: Quota for user `play` for 3600s has been exceeded"
    for _ in range(4):
        with pytest.raises(
            qi.QaIntelUnavailable, match="upstream quota"
        ):  # a quota error is not a breaker failure
            await qi.query_rows("SELECT 1")
    assert qi.circuit_status("qa")["state"] == "CLOSED"
    client.boom = "connection refused"
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await qi.query_rows("SELECT 1")
    assert qi.circuit_status("qa")["state"] == "OPEN"


# --- the handler ---------------------------------------------------------------


class _Source:
    def __init__(
        self,
        row: dict[str, Any] | None,
        hours: list[dict[str, Any]] | None = None,
        fail_history: bool = False,
        fail_hours: bool = False,
    ) -> None:
        self.row, self.hours = row, hours or []
        self.fail_history, self.fail_hours = fail_history, fail_hours
        self.calls: list[Any] = []

    async def test_history(self, org, ns, name, *, since, until=None):
        self.calls.append(("history", name, since))
        if self.fail_history:
            raise TrackerUnavailable("test_history exceeded 8000 ms")
        return self.row

    async def co_failures_in_hours(self, org, ns, name, hours):
        self.calls.append(("co", name, hours))
        if self.fail_hours:
            raise TrackerUnavailable("breaker OPEN")
        return self.hours

    async def failing_tests_per_hour_median(self, org, ns, *, since):
        self.calls.append(("baseline", since))
        return 36  # a live median: K = 5 × 36 = 180

    async def flaky_issues_matching(self, org, ns, name, *, limit=10):
        self.calls.append(("flaky", name))
        return []


class _Repo:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def paths_by_stem(self, org, ns, stems):
        self.calls.append(("stems", stems))
        return [
            {"path": "tests/queries/0_stateless/02345_keeper_session.sql"},
            {"path": "tests/queries/0_stateless/02345_keeper_session.reference"},
        ]

    async def sources_of_tests(self, org, ns, test_paths):
        self.calls.append(("sources", test_paths))
        return ["src/Coordination/KeeperServer.cpp"]

    async def pr_churn_touching(self, org, ns, *, paths, prefix="", since, limit=50):
        self.calls.append(("churn", paths, prefix))
        if paths and paths[0].startswith("src/"):
            return [
                {"pr_number": 7, "title": "Keeper: fix session", "merged_at": datetime(2026, 9, 1)},
                {"pr_number": 8, "title": "Keeper: refactor", "merged_at": datetime(2026, 8, 20)},
            ]
        return [
            {"pr_number": 9, "title": "Test: unflake 02345", "merged_at": datetime(2026, 9, 10)}
        ]


@pytest.fixture
def wired(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(tsig, "_code_repo", lambda: repo)
    monkeypatch.setattr(settings, "code_org", ORG)
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")

    async def _ok(org, ns, role=Role.VIEWER, *, hides_answer=True):
        return ns != "code-secret"

    import server.auth.authz as authz

    monkeypatch.setattr(authz, "row_in_scope", _ok)
    return repo


@pytest.mark.asyncio
async def test_handler_composes_history_cofail_and_churn(monkeypatch, wired) -> None:
    s = _Source(
        _row(
            rerun_flips_total=0,
            flips_total=1,
            fails_total=2,
            fail_hours_all=[datetime(2026, 9, 11, 10), datetime(2026, 9, 12, 10)],
        ),
        hours=[
            {"h": datetime(2026, 9, 11, 10), "other_tests": 726, "other_prs": 37},
            {"h": datetime(2026, 9, 12, 10), "other_tests": 245, "other_prs": 12},
        ],
    )
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_keeper_session")
    )
    assert isinstance(out, TrackerTestSignalOutput) and not out.tracker_unavailable
    assert out.verdict == ts.INFRA and out.window_days == 30 and out.since.endswith(":00:00")
    # K follows the window's baseline: 5 × the median 36 = 180, above the floor 100
    assert out.infra["storm_hours"] == 2 and out.infra["share"] == 1.0 and out.infra["k"] == 180
    assert out.infra["median_failing_tests_per_hour"] == 36 and out.infra["multi_pr_hours"] == []
    assert out.features["fails"] == 2 and out.thresholds["cofail_k"] == 100
    assert (
        s.calls[0][0] == "history"
        and s.calls[1][0] == "baseline"
        and s.calls[2]
        == ("co", "02345_keeper_session", [datetime(2026, 9, 11, 10), datetime(2026, 9, 12, 10)])
    )
    # churn: the .reference sibling is not a home; sources come from the map; both legs counted
    assert out.churn["available"] and out.churn["code_namespace"] == "code-clickhouse"
    assert out.churn["home_paths"] == ["tests/queries/0_stateless/02345_keeper_session.sql"]
    assert out.churn["home_prs"] == 1 and out.churn["source_prs"] == 2 and out.churn["days"] == 54
    assert out.churn["source_recent"][0] == {
        "pr_number": 7,
        "title": "Keeper: fix session",
        "merged_at": "2026-09-01T00:00:00",
    }
    assert wired.calls[0] == ("stems", ["02345_keeper_session"])
    assert set(out.timings_ms) == {
        "history",
        "co_failures",
        "churn",
        "smells",
    }
    assert out.lower_bound == ts.wilson_lower(2, 192)


@pytest.mark.asyncio
async def test_the_served_source_carries_no_checks_password(monkeypatch, wired) -> None:
    """Any viewer reads `source`; the clause's user and password stay in the
    configuration."""
    monkeypatch.setattr(
        settings,
        "tracker_checks_from",
        "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', 'pw-secret')",
    )
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Source(_row()))
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_keeper_session")
    )
    assert out.source == "remoteSecure('play.clickhouse.com', 'default', 'checks', ...)"


@pytest.mark.asyncio
async def test_handler_window_and_churn_switches(monkeypatch, wired) -> None:
    s = _Source(_row())
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    before = datetime.now(UTC).replace(tzinfo=None)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(
            org=ORG,
            namespace=NS,
            test_name="02345_keeper_session",
            window_days=7,
            include_churn=False,
        )
    )
    assert out.verdict == ts.FLAKY and out.window_days == 7 and out.churn == {}
    since = s.calls[0][2]
    # the window start is truncated to the hour so a whole hour of calls shares the cache
    assert since.minute == 0 and since.second == 0
    assert timedelta(days=7) - timedelta(seconds=5) <= before - since <= timedelta(days=7, hours=1)
    assert "churn" not in out.timings_ms and wired.calls == []


@pytest.mark.asyncio
async def test_unavailable_history_is_the_verdict_and_cofail_outage_is_marked(
    monkeypatch, wired
) -> None:
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Source(None, fail_history=True))
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_x")
    )
    assert (
        out.tracker_unavailable
        and out.verdict == ts.UNAVAILABLE
        and "8000 ms" in out.unavailable_reason
    )
    assert out.features == {} and out.churn == {} and out.thresholds["min_runs"] == 5
    # the history read served but the co-failure read did not, and its hours decide between
    # regression and infra: undecided, never a regression read off zero storms
    s = _Source(_row(rerun_flips_total=0, flips_total=1), fail_hours=True)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_x")
    )
    assert out.infra["available"] is False and out.verdict == ts.UNAVAILABLE
    assert out.tracker_unavailable and "breaker OPEN" in out.unavailable_reason
    assert ts.REGRESSION in out.why[0] and ts.INFRA in out.why[0]
    assert out.features["fails"] == 17  # the evidence read is kept
    # a verdict the storm rule never reaches (a rerun flip) stands, with infra marked
    s = _Source(_row(), fail_hours=True)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_x")
    )
    assert out.infra["available"] is False and out.verdict == ts.FLAKY
    assert not out.tracker_unavailable and out.unavailable_reason == ""
    # no runs in the window: silence, never a claim
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Source(None))
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_x")
    )
    assert out.verdict == ts.NO_SIGNAL and out.features["runs"] == 0 and not out.tracker_unavailable


@pytest.mark.asyncio
async def test_churn_leg_refusals_never_fail_the_verdict(monkeypatch, wired) -> None:
    s = _Source(_row())
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    # an unauthorized code namespace
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(
            org=ORG, namespace=NS, test_name="02345_keeper_session", code_namespace="code-secret"
        )
    )
    assert out.verdict == ts.FLAKY and out.churn == ts.churn_unavailable("unauthorized", days=54)
    # a name with no home convention
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="Build ClickHouse")
    )
    assert out.churn["available"] is False and "no home convention" in out.churn["reason"]
    # an integration test reads by directory prefix
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(
            org=ORG, namespace=NS, test_name="test_keeper_multinode/test.py::test_read[1]"
        )
    )
    assert ("churn", [], "tests/integration/test_keeper_multinode/") in wired.calls
    assert (
        out.churn["home_paths"] == ["tests/integration/test_keeper_multinode/"]
        and out.churn["home_prs"] == 1
    )

    # a failing ledger read is a reason, not an exception
    async def _boom(*a, **k):
        raise RuntimeError("ch down")

    monkeypatch.setattr(wired, "paths_by_stem", _boom)
    out = await tsig.handle_tracker_test_signal(
        TrackerTestSignalInput(org=ORG, namespace=NS, test_name="02345_keeper_session")
    )
    assert out.verdict == ts.FLAKY and out.churn["reason"] == "ledger read failed: RuntimeError"


@pytest.mark.asyncio
async def test_tracker_flaky_payload_is_unchanged_unless_signal_is_asked_for(
    monkeypatch, wired
) -> None:
    s = _Source(_row())
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    out = await h.handle_tracker_flaky(
        TrackerFlakyInput(org=ORG, namespace=NS, test_name="02345_keeper_session")
    )
    dumped = out.model_dump(mode="json")
    assert "signal" not in dumped and dumped["verdict"] == "no_flaky_signal"
    assert set(dumped) == {
        "test_name",
        "known_flaky",
        "open_issues",
        "issues",
        "verdict",
        "tracker_unavailable",
    }
    assert [c[0] for c in s.calls] == ["flaky"]
    out = await h.handle_tracker_flaky(
        TrackerFlakyInput(
            org=ORG,
            namespace=NS,
            test_name="02345_keeper_session",
            include_signal=True,
            window_days=14,
        )
    )
    dumped = out.model_dump(mode="json")
    assert dumped["signal"]["verdict"] == ts.FLAKY and dumped["signal"]["window_days"] == 14
    assert [c[0] for c in s.calls] == ["flaky", "flaky", "history", "baseline", "co"]


def test_op_is_registered_experimental_viewer_and_described() -> None:
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    op = OPERATIONS_BY_NAME["tracker.test_signal"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    assert op.budget_p99_ms == 8000 and not op.skip_namespace_authz
    assert "namespace" in op.input_model.model_fields
    assert "no_signal" in TOOL_DESCRIPTIONS["tracker.test_signal"]
    assert "silence, not health" in TOOL_DESCRIPTIONS["tracker.test_signal"]


def test_settings_cite_the_decision_and_default_as_measured() -> None:
    assert (
        settings.tracker_live_checks_budget_ms == 8000
        and settings.tracker_test_signal_window_days == 30
    )
    assert (
        settings.tracker_test_signal_churn_days == 54
        and settings.tracker_test_signal_cofail_k == 100
    )
    assert settings.tracker_test_signal_cofail_multiple == 5.0
    # No deployment's CI is loom's default
    from server.settings import Settings

    assert Settings.model_fields["tracker_checks_from"].default == ""
    assert Settings.model_fields["tracker_checks_repo"].default == ""


@pytest.mark.asyncio
async def test_sdk_posts_the_op(monkeypatch) -> None:
    from loom.tracker import TrackerClient

    posted: list[tuple[str, dict[str, Any]]] = []

    class _T:
        async def post(self, path, body):
            posted.append((path, body))
            return {"verdict": "flaky"}

    class _A:
        org, namespace, transport = ORG, "personal", _T()

    c = TrackerClient(_A())  # type: ignore[arg-type]
    out = await c.test_signal("02345_keeper_session", namespace=NS, window_days=14)
    assert out == {"verdict": "flaky"}
    assert posted[-1] == (
        "/v1/tracker.test_signal",
        {
            "org": ORG,
            "namespace": NS,
            "test_name": "02345_keeper_session",
            "window_days": 14,
            "code_namespace": "",
            "include_churn": True,
            "pr_number": 0,
            "include_smells": True,
        },
    )
    await c.flaky("02345_keeper_session", namespace=NS)
    assert posted[-1][1] == {
        "org": ORG,
        "namespace": NS,
        "test_name": "02345_keeper_session",
        "limit": 10,
    }
    await c.flaky("02345_keeper_session", namespace=NS, include_signal=True)
    assert posted[-1][1]["include_signal"] is True and posted[-1][1]["window_days"] == 0


def test_a_failed_baseline_read_leaves_a_storm_only_verdict_undecided() -> None:
    """Without the baseline K falls to its floor, which counts more hours as
    storms than the window's own median would: a verdict that holds only
    with those storms (infra) is undecided, not a clearance of the change."""
    th = ts.Thresholds()
    storm = _row(
        rerun_flips_total=0,
        flips_total=1,
        fails_total=2,
        fail_hours_all=[datetime(2026, 9, 11, 10), datetime(2026, 9, 12, 10)],
    )
    feat = ts.features(storm)
    verdict = ts.verdict(feat, 2, th, storm_k=th.cofail_k)[0]
    assert verdict == ts.INFRA
    infra = {"k": th.cofail_k, "storm_hours": 2, "available": True}
    assert tsig._storm_unread(feat, infra, th, verdict) == ""  # the baseline was read
    unread = tsig._storm_unread(
        feat, {**infra, "baseline_unavailable": "breaker open"}, th, verdict
    )
    assert "baseline read failed" in unread and "undecided" in unread
    # no storm counted: the floor K changed nothing
    calm = {**infra, "storm_hours": 0, "baseline_unavailable": "breaker open"}
    assert tsig._storm_unread(feat, calm, th, ts.verdict(feat, 0, th)[0]) == ""
