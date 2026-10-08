"""Loom's own tables of the tracker plane: the per-test daily roll-up of the
public CI history and its two companions.

`loom.qa_test_daily` holds one row per (test, day), `loom.qa_fail_hours` the
co-failure baseline per hour, `loom.qa_rollup_days` the worker's ledger of
what is rolled and how far. The worker writes them; `tracker.test_signal` reads
them as key-range point reads before it touches the remote. Every read is
`FINAL` on ReplacingMergeTree(version): a partial day is re-rolled every
pass and the newest roll must win.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, TypedDict, cast

from server.core.datetimes import naive_utc_now as _now
from server.core.repo_base import _CHClient, _default_client

TRACKER_TEST_DAILY_COLS: dict[str, Any] = {
    "org": "",
    "qa_namespace": "",
    "test_name": "",
    "day": None,
    "runs": 0,
    "fails": 0,
    "prs": 0,
    "groups": 0,
    "steps": 0,
    "flips": 0,
    "same_commit_flips": 0,
    "branch_fails": 0,
    "last_true_fail": None,
    "last_true_fail_head": "",
    "last_fail_at": None,
    "first_run_at": None,
    "fail_hours": [],
    "fail_hour_prs": [],
    "fail_prs": [],
    "fail_pr_fails": [],
    "fail_pr_tail": [],
    "fail_pr_last": [],
    "partial": 0,
    "rolled_until": None,
    "rolled_at": None,
    "created_at": None,
    "version": 0,
}
TRACKER_FAIL_HOURS_COLS: dict[str, Any] = {
    "org": "",
    "qa_namespace": "",
    "hour": None,
    "day": None,
    "failing_tests": 0,
    "failing_prs": 0,
    "partial": 0,
    "created_at": None,
    "version": 0,
}
TRACKER_ROLLUP_DAYS_COLS: dict[str, Any] = {
    "org": "",
    "qa_namespace": "",
    "day": None,
    "partial": 0,
    "rolled_until": None,
    "tests": 0,
    "failing_tests": 0,
    "runs": 0,
    "statements": 0,
    "remote_ms": 0,
    "source": "",
    "rolled_at": None,
    "created_at": None,
    "version": 0,
}
_TABLES = {
    "tracker_test_daily": TRACKER_TEST_DAILY_COLS,
    "qa_fail_hours": TRACKER_FAIL_HOURS_COLS,
    "qa_rollup_days": TRACKER_ROLLUP_DAYS_COLS,
}


class DailyRow(TypedDict):
    """One test's rolled day, as the window read selects it."""

    day: date
    runs: int
    fails: int
    prs: int
    groups: int
    steps: int
    flips: int
    same_commit_flips: int
    branch_fails: int
    last_true_fail: datetime
    last_true_fail_head: str
    last_fail_at: datetime
    first_run_at: datetime
    fail_hours: list[datetime]
    fail_hour_prs: list[tuple[datetime, int]]
    fail_prs: list[int]
    fail_pr_fails: list[int]
    fail_pr_tail: list[int]
    fail_pr_last: list[int]
    partial: int
    rolled_until: datetime


class HourStat(TypedDict):
    """One rolled hour's distinct failing tests and PR heads."""

    hour: datetime
    failing_tests: int
    failing_prs: int


class TestNameMatch(TypedDict):
    """A rolled test whose name matches, with its window's counts."""

    test_name: str
    runs_in_window: int
    fails_in_window: int
    matches: int


def version_now() -> int:
    return time.time_ns()


def _check_rows(table: str, rows: list[dict[str, Any]]) -> None:
    cols = _TABLES[table]
    for r in rows:
        unknown = set(r) - set(cols)
        missing = set(cols) - set(r)
        if unknown or missing:
            raise ValueError(
                f"{table}: unknown columns {sorted(unknown)}, missing {sorted(missing)}"
            )


class TrackerDailyRepo:
    def __init__(self, ch: _CHClient | None = None) -> None:
        self._ch = ch or _default_client()

    # -- writes (the worker) ---------------------------------------------------

    async def insert_daily(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            _check_rows("tracker_test_daily", rows)
            await self._ch.insert("loom.qa_test_daily", rows)

    async def insert_hours(self, rows: list[dict[str, Any]]) -> None:
        if rows:
            _check_rows("qa_fail_hours", rows)
            await self._ch.insert("loom.qa_fail_hours", rows)

    async def insert_rolled_day(self, row: dict[str, Any]) -> None:
        full = {**TRACKER_ROLLUP_DAYS_COLS, **row}
        for k in ("rolled_at", "created_at"):
            full[k] = full[k] or _now()
        full["version"] = full["version"] or version_now()
        _check_rows("qa_rollup_days", [full])
        await self._ch.insert("loom.qa_rollup_days", [full])

    # -- reads (the worker's plan, the op) -------------------------------------

    async def rolled_days(
        self, org: str, ns: str, since_day: date, until_day: date
    ) -> dict[date, tuple[bool, datetime]]:
        """day → (partial, rolled_until) for the days the ledger holds in
        [since_day, until_day]."""
        rows = await self._ch.query_all(
            """
            SELECT day, partial, rolled_until
            FROM loom.qa_rollup_days FINAL
            WHERE org = {org:String} AND qa_namespace = {ns:String}
              AND day >= {d0:Date} AND day <= {d1:Date}
            ORDER BY day
            """,
            {"org": org, "ns": ns, "d0": since_day, "d1": until_day},
        )
        out: dict[date, tuple[bool, datetime]] = {}
        for r in rows:
            d = r.get("day")
            day = d if isinstance(d, date) else date.fromisoformat(str(d))
            until = r.get("rolled_until")
            out[day] = (
                bool(int(r.get("partial") or 0)),
                until if isinstance(until, datetime) else datetime(1970, 1, 1),
            )
        return out

    async def window(
        self, org: str, ns: str, test_name: str, since_day: date, until_day: date
    ) -> list[DailyRow]:
        """One test's rows in [since_day, until_day], oldest first — a key
        range of ≤ 31 adjacent rows."""
        rows = await self._ch.query_all(
            """
            SELECT day, runs, fails, prs, groups, steps, flips, same_commit_flips, branch_fails,
                   last_true_fail, last_true_fail_head, last_fail_at, first_run_at,
                   fail_hours, fail_hour_prs, fail_prs, fail_pr_fails, fail_pr_tail, fail_pr_last,
                   partial, rolled_until
            FROM loom.qa_test_daily FINAL
            WHERE org = {org:String} AND qa_namespace = {ns:String}
              AND test_name = {t:String} AND day >= {d0:Date} AND day <= {d1:Date}
            ORDER BY day
            """,
            {"org": org, "ns": ns, "t": test_name, "d0": since_day, "d1": until_day},
        )
        return cast(list[DailyRow], rows)

    async def hour_stats(self, org: str, ns: str, hours: list[datetime]) -> list[HourStat]:
        """Per hour in `hours`: the distinct tests that failed in it and the
        PR heads they failed on (the co-failure leg's facts, from the roll-up)."""
        want = sorted({h.replace(minute=0, second=0, microsecond=0) for h in hours})
        if not want:
            return []
        rows = await self._ch.query_all(
            """
            SELECT hour, failing_tests, failing_prs
            FROM loom.qa_fail_hours FINAL
            WHERE org = {org:String} AND qa_namespace = {ns:String}
              AND hour IN {hours:Array(DateTime)}
            ORDER BY hour
            """,
            {"org": org, "ns": ns, "hours": want[:1024]},
        )
        return cast(list[HourStat], rows)

    async def hour_median(self, org: str, ns: str, since: datetime, until: datetime) -> int:
        """The window's median distinct failing tests per hour, over the hours
        the roll-up holds (hours with no failure have no row); 0 when the
        roll-up holds no hour of the window."""
        row = await self._ch.query_one(
            """
            SELECT toUInt32(quantileExact(0.5)(failing_tests)) AS median_tests, count() AS hours
            FROM loom.qa_fail_hours FINAL
            WHERE org = {org:String} AND qa_namespace = {ns:String}
              AND hour >= {h0:DateTime} AND hour < {h1:DateTime}
            """,
            {
                "org": org,
                "ns": ns,
                "h0": since.replace(microsecond=0),
                "h1": until.replace(microsecond=0),
            },
        )
        if not row or int(row.get("hours") or 0) == 0:
            return 0
        return int(row.get("median_tests") or 0)

    async def test_names_like(
        self,
        org: str,
        ns: str,
        needle: str,
        since_day: date,
        until_day: date,
        *,
        prefix: bool,
        limit: int = 5,
    ) -> list[TestNameMatch]:
        """The rolled tests of [since_day, until_day] whose name starts with
        (`prefix`) or contains `needle`, most runs first —
        `runs_in_window`, `fails_in_window` and `matches`, the count of every
        matching test (aliases never reuse a source column's name). The prefix read is a
        key range (8 ms, 10 k rows for `02346_text_index`); the substring read
        scans the namespace's rows (106 ms, 2.7 M rows), so callers try it only
        when the prefix read found nothing."""
        match = (
            "startsWith(test_name, {n:String})"
            if prefix
            else "positionCaseInsensitive(test_name, {n:String}) > 0"
        )
        rows = await self._ch.query_all(
            f"""
            SELECT test_name, sum(runs) AS runs_in_window, sum(fails) AS fails_in_window,
                   count() OVER () AS matches
            FROM loom.qa_test_daily FINAL
            WHERE org = {{org:String}} AND qa_namespace = {{ns:String}}
              AND day >= {{d0:Date}} AND day <= {{d1:Date}} AND {match}
            GROUP BY test_name
            ORDER BY runs_in_window DESC, test_name
            LIMIT {{lim:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "n": needle,
                "d0": since_day,
                "d1": until_day,
                "lim": max(1, int(limit)),
            },
        )
        return cast(list[TestNameMatch], rows)


__all__ = [
    "TRACKER_FAIL_HOURS_COLS",
    "TRACKER_ROLLUP_DAYS_COLS",
    "TRACKER_TEST_DAILY_COLS",
    "DailyRow",
    "HourStat",
    "TestNameMatch",
    "TrackerDailyRepo",
    "version_now",
]
