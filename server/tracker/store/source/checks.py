"""A test's own CI history and the day fold behind loom.qa_test_daily, read
from the tracker's CI-checks table (`checks_config`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from server.settings import settings
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.conventions import checks_config


class CheckReads(SourceBase):
    """Readers of the CI-checks history."""

    # -- a test's own CI history ----------------------------------------------------
    # `checks` may be public CI behind a remoteSecure table function. If it
    # carries no `is_private` and no repository column, the privacy predicate
    # does not apply, and a namespace whose repositories do not include the
    # public one has nothing to read here.
    # The table function is refused under `readonly = 1`; the client already
    # runs under `readonly = 2` (SELECT only, still), and these statements
    # also carry it so they never depend on the client's session setting.

    # Which CI history and which repository it is are the
    # tracker's configuration (`checks_config`), not constants of this class
    _CHECKS_SETTINGS = {"readonly": 2}

    def _checks_from(self) -> str:
        return checks_config(self.org, self.namespace)[0]

    @staticmethod
    def _checks_budget() -> int:
        return int(settings.tracker_live_checks_budget_ms)

    def _admit_checks(self) -> None:
        frm, repo = checks_config(self.org, self.namespace)
        if not frm:
            raise TrackerUnavailable(
                f"{self.namespace} has no CI checks history configured (the tracker's `checks` "
                "table, its source entry's `checks.from`, or LOOM_TRACKER_CHECKS_FROM)"
            )
        if repo and repo not in self.repos():
            raise TrackerUnavailable(
                f"{self.namespace} does not serve {repo}: the checks history is that "
                "repository's CI only"
            )

    async def test_history(
        self, org: str, ns: str, test_name: str, *, since: datetime, until: datetime | None = None
    ) -> dict[str, Any] | None:
        """One aggregated row of a test's runs in [since, until): runs, failures,
        PR heads, branch-head failures, the flips of every (PR, check)
        sequence in time order, the commits that both failed and passed, the
        hours it failed in, and per failing PR head its failures, the trailing
        failure run of its failing check sequence and whether that sequence's
        latest run failed (the previous-outcome streak). Grouped
        first per (PR, check, commit) so a rerun is visible; the remote ships
        filtered rows, the QA side folds them. `until` None = an hour past
        now (the live tail of a roll-up read stops at the present). None when
        the window holds no run."""
        self._admit_checks()
        name = (test_name or "").strip()
        if not name:
            return None
        from server.tracker.domain.test_signal import FAIL_STATUSES, MAX_FAIL_HOURS

        sql = f"""
            SELECT sum(runs) AS runs_total, sum(fails) AS fails_total,
                   uniqExact(pr) AS prs_total, uniqExactIf(pr, fails > 0) AS failing_prs,
                   sumIf(fails, pr = 0) AS branch_fails,
                   maxIf(last_fail, fails > 0 AND pr = 0) AS last_true_fail,
                   argMaxIf(head, last_fail, fails > 0 AND pr = 0) AS last_true_fail_head,
                   max(last_fail) AS last_fail_at, min(first_run) AS first_run_at,
                   sum(flips) AS flips_total, sum(runs - 1) AS steps_total,
                   sum(rerun_flips) AS rerun_flips_total,
                   arraySlice(arraySort(groupUniqArrayArray(fail_hours)), -{int(MAX_FAIL_HOURS)})
                       AS fail_hours_all,
                   arrayDistinct(groupArrayArray(arrayMap(h -> (h, pr), fail_hours)))
                       AS fail_hour_prs,
                   groupArrayIf((pr, fails, tail, last_failed), fails > 0 AND pr > 0) AS per_pr_raw
            FROM (
                WITH test_status IN {{fail_statuses:Array(String)}} AS f
                SELECT pull_request_number AS pr, check_name AS chk, anyLast(head_ref) AS head,
                       count() AS runs, countIf(f) AS fails,
                       maxIf(check_start_time, f) AS last_fail, min(check_start_time) AS first_run,
                       arraySort(x -> x.1, groupArray((check_start_time, f))) AS seq,
                       arraySum(arrayMap((a, b) -> a.2 != b.2,
                                         arrayPopBack(seq), arrayPopFront(seq))) AS flips,
                       length(arrayIntersect(groupUniqArrayIf(commit_sha, f),
                                             groupUniqArrayIf(commit_sha, test_status = 'OK')))
                           AS rerun_flips,
                       groupUniqArrayIf(toStartOfHour(check_start_time), f) AS fail_hours,
                       arrayFirstIndex(x -> NOT x.2, arrayReverse(seq)) AS first_pass_from_end,
                       toUInt32(if(first_pass_from_end = 0, runs, first_pass_from_end - 1)) AS tail,
                       toUInt8(seq[-1].2) AS last_failed
                FROM {self._checks_from()}
                WHERE test_name = {{t:String}} AND check_start_time >= {{since:DateTime}}
                  AND check_start_time < {{until:DateTime}}
                GROUP BY pr, chk
            )
        """
        stop = until or (datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
        params = {
            "t": name,
            "since": since.replace(microsecond=0),
            "until": stop.replace(microsecond=0),
            "fail_statuses": list(FAIL_STATUSES),
        }
        rows = await self._rows(
            "test_history",
            sql,
            params,
            cache_key=[name, params["since"], params["until"]],
            budget_ms=self._checks_budget(),
            query_settings=self._CHECKS_SETTINGS,
        )
        row = rows[0] if rows else None
        if not row or int(row.get("runs_total") or 0) == 0:
            return None
        from server.tracker.domain.test_daily import per_pr_arrays

        return {**row, **per_pr_arrays(row.get("per_pr_raw"))}

    async def failing_tests_per_hour_median(self, org: str, ns: str, *, since: datetime) -> int:
        """The window's median count of distinct failing tests per hour — the
        baseline a storm hour is an outlier of (41 at the median, 155 at p90,
        797 at p99 over 30 days). One statement per window start,
        shared by every test through the cache; 0 when nothing failed."""
        self._admit_checks()
        from server.tracker.domain.test_signal import FAIL_STATUSES

        sql = f"""
            SELECT toUInt32(quantileExact(0.5)(tests)) AS median_tests, count() AS hours
            FROM (
                SELECT toStartOfHour(check_start_time) AS h, uniqExact(test_name) AS tests
                FROM {self._checks_from()}
                WHERE test_status IN {{fail_statuses:Array(String)}}
                  AND check_start_time >= {{since:DateTime}}
                GROUP BY h
            )
        """
        params = {"since": since.replace(microsecond=0), "fail_statuses": list(FAIL_STATUSES)}
        rows = await self._rows(
            "fail_hour_baseline",
            sql,
            params,
            cache_key=[params["since"]],
            budget_ms=self._checks_budget(),
            query_settings=self._CHECKS_SETTINGS,
        )
        return int(rows[0].get("median_tests") or 0) if rows else 0

    async def co_failures_in_hours(
        self, org: str, ns: str, test_name: str, hours: list[datetime]
    ) -> list[dict[str, Any]]:
        """Per hour in `hours`: how many OTHER tests failed and across how many
        PRs — the co-failure leg. The hour list is the
        test's own failing hours (≤ MAX_FAIL_HOURS), so the remote reads those
        hours by its time key and its failure rows only."""
        self._admit_checks()
        from server.tracker.domain.test_signal import FAIL_STATUSES, MAX_FAIL_HOURS

        trunc = {h.replace(minute=0, second=0, microsecond=0) for h in hours}
        want = sorted(trunc)[-MAX_FAIL_HOURS:]
        name = (test_name or "").strip()
        if not want or not name:
            return []
        sql = f"""
            SELECT toStartOfHour(check_start_time) AS h,
                   uniqExact(test_name) AS other_tests, uniqExact(pull_request_number) AS other_prs
            FROM {self._checks_from()}
            WHERE test_status IN {{fail_statuses:Array(String)}}
              AND toStartOfHour(check_start_time) IN {{hours:Array(DateTime)}}
              AND test_name != {{t:String}}
            GROUP BY h
            ORDER BY h
        """
        params = {"t": name, "hours": want, "fail_statuses": list(FAIL_STATUSES)}
        return await self._rows(
            "co_failures",
            sql,
            params,
            cache_key=[name, [h.isoformat() for h in want]],
            budget_ms=self._checks_budget(),
            query_settings=self._CHECKS_SETTINGS,
        )

    # -- the day fold behind loom.qa_test_daily -------------------------------------
    # Two statements per day over EVERY test at once, run whole at the remote
    # (`remoteSecure(host, view(...))`): two-stage aggregation would ship one
    # partial state per (test, PR, check) group — ~100 M a day — to the QA
    # service, and the exact count of those groups cost 18 s at the remote
    # where the uniq estimate costs 0.7 s. The failure fields come from the
    # failing (PR, check) sequences only, ~100 k rows of the day's 120 M.

    @staticmethod
    def _checks_daily_budget() -> int:
        return int(settings.tracker_test_daily_budget_ms)

    def _checks_pushdown(self, inner: str) -> str:
        """The FROM clause that runs `inner` whole at the remote when the
        checks clause is the five-argument table function; over the QA view
        (or any other clause) `inner` is a plain subquery over it."""
        from server.tracker.domain.test_daily import parse_remote_clause

        remote = parse_remote_clause(self._checks_from())
        if remote is None:
            return f"({inner.replace('__CHECKS__', self._checks_from())})"
        return remote.pushdown(inner.replace("__CHECKS__", remote.qualified_table))

    async def test_day_fold(
        self, org: str, ns: str, *, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        """Per test that ran in [since, until): runs, failures, distinct PR
        heads, an estimate of its (PR, check) groups (`uniq`; the flip-rate
        denominator is runs − groups) and its first run — 68 k rows, 0.7 s
        at the remote for a 120 M-row day."""
        self._admit_checks()
        from server.tracker.domain.test_signal import FAIL_STATUSES

        inner = """WITH test_status IN {fail_statuses:Array(String)} AS f
            SELECT test_name, count() AS runs, countIf(f) AS fails,
                   uniqExact(pull_request_number) AS prs,
                   uniq((pull_request_number, check_name)) AS groups,
                   min(check_start_time) AS first_run_at
            FROM __CHECKS__
            WHERE check_start_time >= {d0:DateTime} AND check_start_time < {d1:DateTime}
            GROUP BY test_name"""
        sql = f"SELECT * FROM {self._checks_pushdown(inner)}"
        params = {
            "d0": since.replace(microsecond=0),
            "d1": until.replace(microsecond=0),
            "fail_statuses": list(FAIL_STATUSES),
        }
        return await self._rows(
            "test_day_fold",
            sql,
            params,
            cache_key=[params["d0"], params["d1"]],
            budget_ms=self._checks_daily_budget(),
            ttl_s=1.0,
            query_settings=self._CHECKS_SETTINGS,
        )

    async def test_day_sequences(
        self, org: str, ns: str, *, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        """Per test that FAILED in [since, until): the flips and same-commit
        flips of its failing (PR, check) sequences, branch-head failures, the
        hours it failed in with the PR heads per hour, and per failing PR head
        (fails, trailing failure run, latest run failed) — read from the rows
        of the failing sequences only."""
        self._admit_checks()
        from server.tracker.domain.test_signal import FAIL_STATUSES

        inner = """WITH test_status IN {fail_statuses:Array(String)} AS f
            SELECT test_name, sum(flips) AS flips, sum(rerun_flips) AS same_commit_flips,
                   sumIf(fails, pr = 0) AS branch_fails, maxIf(last_fail, pr = 0) AS last_true_fail,
                   argMaxIf(head, last_fail, pr = 0) AS last_true_fail_head,
                   max(last_fail) AS last_fail_at,
                   arraySort(groupUniqArrayArray(fail_hours)) AS fail_hours_all,
                   arrayDistinct(groupArrayArray(arrayMap(h -> (h, pr), fail_hours)))
                       AS fail_hour_prs,
                   groupArrayIf((pr, fails, tail, last_failed), pr > 0) AS per_pr_raw
            FROM (
                SELECT test_name, pr, chk, anyLast(head_ref) AS head, count() AS runs,
                       countIf(f) AS fails, maxIf(check_start_time, f) AS last_fail,
                       arraySort(x -> x.1, groupArray((check_start_time, f))) AS seq,
                       arraySum(arrayMap((a, b) -> a.2 != b.2,
                                         arrayPopBack(seq), arrayPopFront(seq))) AS flips,
                       length(arrayIntersect(groupUniqArrayIf(commit_sha, f),
                                             groupUniqArrayIf(commit_sha, test_status = 'OK')))
                           AS rerun_flips,
                       groupUniqArrayIf(toStartOfHour(check_start_time), f) AS fail_hours,
                       arrayFirstIndex(x -> NOT x.2, arrayReverse(seq)) AS first_pass_from_end,
                       toUInt32(if(first_pass_from_end = 0, runs, first_pass_from_end - 1)) AS tail,
                       toUInt8(seq[-1].2) AS last_failed
                FROM __CHECKS__
                WHERE check_start_time >= {d0:DateTime} AND check_start_time < {d1:DateTime}
                  AND (test_name, pull_request_number, check_name) IN (
                      SELECT test_name, pull_request_number, check_name FROM __CHECKS__
                      WHERE check_start_time >= {d0:DateTime} AND check_start_time < {d1:DateTime}
                        AND f)
                GROUP BY test_name, pull_request_number AS pr, check_name AS chk)
            GROUP BY test_name"""
        sql = f"SELECT * FROM {self._checks_pushdown(inner)}"
        params = {
            "d0": since.replace(microsecond=0),
            "d1": until.replace(microsecond=0),
            "fail_statuses": list(FAIL_STATUSES),
        }
        return await self._rows(
            "test_day_sequences",
            sql,
            params,
            cache_key=[params["d0"], params["d1"]],
            budget_ms=self._checks_daily_budget(),
            ttl_s=1.0,
            query_settings=self._CHECKS_SETTINGS,
        )
