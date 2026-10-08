"""Pure helpers for the per-test daily roll-up and the CI prior read from it.

The roll-up is one row per (test, day) in `loom.qa_test_daily`, built by the
`tracker_test_daily` worker from two bounded statements over the public CI table
— the day fold (runs, fails, PR heads, an estimate of the (PR, check)
groups) and the failing sequences (flips, same-commit flips, failing hours
and PR heads, the trailing failure run per PR head) — and folded back here
into the one history row `tracker.test_signal`'s rules read (`test_signal.features`),
so the verdict vocabulary and thresholds of `test_signal` are untouched.

Two cheap features on top of it:

- **previous-outcome streak** per (PR head, test): a failure that follows a
  failure is a *subsequent* failure, so after a red run the next run is
  expected red until a pass is observed;
- **static smell flags** from the test's own source: sleeps, threads,
  wall-clock time, network and randomness in the test body, plus the churn of
  the code it covers (the ledger's, `test_signal`'s churn leg: loom has no
  coverage). The flags are counted, never weighted: no labels exist to
  calibrate a weight.

Everything here is pure: rows in, rows out; the readers live in
`server.tracker.store.source`, the loom tables in
`server.tracker.store.daily`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from server.core.datetimes import to_naive_utc

EPOCH = datetime(1970, 1, 1)

SMELL_PATTERNS: dict[str, re.Pattern[str]] = {
    # waiting on time instead of on a condition (the most common flaky-test fix)
    "async_wait": re.compile(
        r"\bsleep\w*\b|\btimeout\b|\bretry\w*\b|\bwait_for\w*\b|\bpoll\w*\b", re.I
    ),
    # threads, concurrent queries, background processing (concurrency 20 %)
    "concurrency": re.compile(
        r"\bthread\w*\b|\bparallel\w*\b|\bconcurren\w*\b|\basync\w*\b|\bbackground\b|\bmutex\b|max_threads",
        re.I,
    ),
    # the wall clock in the assertion path
    "time": re.compile(
        r"\bnow(?:64)?\s*\(|\btoday\s*\(|time\.time|datetime\.now|current_timestamp"
        r"|yesterday\s*\(",
        re.I,
    ),
    # a mystery guest: another process, a socket, a service
    "network": re.compile(
        r"\bhttp\w*\b|\burl\b|\bsocket\w*\b|\bconnect\w*\b|\bport\b|\bremote\w*\b|\bs3\b|\bzookeeper\b"
        r"|\bkeeper\b|\bkafka\b|\bcluster\w*\b|\bpostgres\w*\b|\bmysql\b|\brabbitmq\b|\bnats\b",
        re.I,
    ),
    # randomness in the data or the order
    "random": re.compile(r"\brand\w*\s*\(|\brandom\w*\b|\bshuffle\w*\b", re.I),
}
SMELL_CATEGORIES: tuple[str, ...] = tuple(SMELL_PATTERNS)

_REMOTE_RE = re.compile(
    r"^\s*remoteSecure\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)\s*$"
)


@dataclass(frozen=True)
class RemoteChecks:
    host: str
    database: str
    table: str
    user: str
    password: str

    def pushdown(self, inner_sql: str) -> str:
        """A FROM clause that runs `inner_sql` (over `{db}.{table}`) whole at
        the remote: `remoteSecure(host, view(...), user, pw)`. Two-stage
        aggregation would ship one partial state per (test, PR, check) group
        — ~100 M a day — back to the QA service; the view ships the folded
        rows (68 k a day, 10 MB)."""
        return f"remoteSecure('{self.host}', view({inner_sql}), '{self.user}', '{self.password}')"

    @property
    def qualified_table(self) -> str:
        return f"{self.database}.{self.table}"


def parse_remote_clause(clause: str) -> RemoteChecks | None:
    """`remoteSecure('host', 'db', 'table', 'user', 'pw')` → its parts; None
    for any other clause (the QA view itself, a plain table)."""
    m = _REMOTE_RE.match(clause or "")
    if not m:
        return None
    return RemoteChecks(*m.groups())


# --- the worker's plan --------------------------------------------------------------


@dataclass(frozen=True)
class DayPlan:
    day: date
    partial: bool
    since: datetime
    until: datetime


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def plan_days(
    rolled: dict[date, tuple[bool, datetime]],
    now: datetime,
    *,
    backfill_days: int,
    lag_hours: float,
    max_days: int,
) -> list[DayPlan]:
    """Which days this pass rolls, newest first: today (partial, up to the
    current hour), then every day of the backfill window that is missing or
    still partial. A day is final once `lag_hours` have passed since it
    ended — rows arrive when a check finishes, not when it starts — and a
    final day is never re-rolled. `rolled` maps day → (partial, rolled_until)."""
    hour_now = now.replace(minute=0, second=0, microsecond=0)
    today = now.date()
    out: list[DayPlan] = []
    for back in range(0, max(0, int(backfill_days)) + 1):
        day = today - timedelta(days=back)
        start, end = day_bounds(day)
        final_due = now >= end + timedelta(hours=float(lag_hours))
        until = end if final_due else min(end, hour_now)
        if until <= start:
            continue  # nothing of the day has happened yet (just past midnight)
        prev = rolled.get(day)
        if prev is not None:
            prev_partial, prev_until = prev
            if not prev_partial:
                continue  # final: settled
            if not final_due and prev_until >= until:
                continue  # partial and nothing new to roll yet
        out.append(DayPlan(day=day, partial=not final_due, since=start, until=until))
        if len(out) >= max(1, int(max_days)):
            break
    return out


# --- rows of a day ------------------------------------------------------------------


def _dt(v: Any) -> datetime:
    return to_naive_utc(v) if isinstance(v, datetime) else EPOCH


def _pairs(v: Any) -> list[tuple[datetime, int]]:
    out: list[tuple[datetime, int]] = []
    for p in v or []:
        try:
            out.append((_dt(p[0]), int(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _per_pr(raw: Any) -> tuple[list[int], list[int], list[int], list[int]]:
    """(pr, fails, tail, last_failed) tuples of one test's failing (PR, check)
    sequences, folded per PR head: failures summed, the longest trailing run
    kept, `last` 1 when any failing check's latest run on that head failed."""
    acc: dict[int, list[int]] = {}
    for t in raw or []:
        try:
            pr, fails, tail, last = int(t[0]), int(t[1]), int(t[2]), int(bool(t[3]))
        except (TypeError, ValueError, IndexError):
            continue
        if pr <= 0:
            continue
        cur = acc.setdefault(pr, [0, 0, 0])
        cur[0] += fails
        cur[1] = max(cur[1], tail)
        cur[2] = max(cur[2], last)
    prs = sorted(acc)
    return (prs, [acc[p][0] for p in prs], [acc[p][1] for p in prs], [acc[p][2] for p in prs])


def per_pr_arrays(raw: Any) -> dict[str, list[int]]:
    """The four parallel per-PR-head arrays a roll-up row and the live row
    both carry, from the reader's `per_pr_raw` tuples."""
    prs, fails, tail, last = _per_pr(raw)
    return {"fail_prs": prs, "fail_pr_fails": fails, "fail_pr_tail": tail, "fail_pr_last": last}


def merge_day(
    fold_rows: list[dict[str, Any]],
    seq_rows: list[dict[str, Any]],
    *,
    org: str,
    tracker_namespace: str,
    day: date,
    partial: bool,
    rolled_until: datetime,
    version: int,
    rolled_at: datetime,
) -> list[dict[str, Any]]:
    """Full `loom.qa_test_daily` rows for one day from the fold statement
    (every test that ran) and the sequences statement (every test that
    failed). A test in the second and not the first cannot happen (a failure
    is a run); a test in the first only has zero failure fields."""
    seqs = {str(r.get("test_name") or ""): r for r in seq_rows}
    out: list[dict[str, Any]] = []
    for r in fold_rows:
        name = str(r.get("test_name") or "")
        if not name:
            continue
        runs = int(r.get("runs") or 0)
        groups = min(int(r.get("groups") or 0), runs)
        s = seqs.get(name, {})
        prs, pr_fails, pr_tail, pr_last = _per_pr(s.get("per_pr_raw"))
        out.append(
            {
                "org": org,
                "qa_namespace": tracker_namespace,
                "test_name": name,
                "day": day,
                "runs": runs,
                "fails": int(r.get("fails") or 0),
                "prs": int(r.get("prs") or 0),
                "groups": groups,
                "steps": max(0, runs - groups),
                "flips": int(s.get("flips") or 0),
                "same_commit_flips": int(s.get("same_commit_flips") or 0),
                "branch_fails": int(s.get("branch_fails") or 0),
                "last_true_fail": _dt(s.get("last_true_fail")),
                "last_true_fail_head": str(s.get("last_true_fail_head") or ""),
                "last_fail_at": _dt(s.get("last_fail_at")),
                "first_run_at": _dt(r.get("first_run_at")),
                "fail_hours": sorted({_dt(h) for h in (s.get("fail_hours_all") or [])}),
                "fail_hour_prs": sorted(set(_pairs(s.get("fail_hour_prs")))),
                "fail_prs": prs,
                "fail_pr_fails": pr_fails,
                "fail_pr_tail": pr_tail,
                "fail_pr_last": pr_last,
                "partial": 1 if partial else 0,
                "rolled_until": rolled_until,
                "rolled_at": rolled_at,
                "created_at": rolled_at,
                "version": version,
            }
        )
    return out


def hour_rows(
    daily_rows: list[dict[str, Any]],
    *,
    org: str,
    tracker_namespace: str,
    day: date,
    partial: bool,
    version: int,
    created_at: datetime,
) -> list[dict[str, Any]]:
    """`loom.qa_fail_hours` rows of the day from its test rows: per hour the
    distinct tests that failed in it and the distinct PR heads (branch heads
    counted as one head, as the live co-failure read counts them)."""
    tests: dict[datetime, int] = {}
    prs: dict[datetime, set[int]] = {}
    for r in daily_rows:
        for h in r.get("fail_hours") or []:
            tests[h] = tests.get(h, 0) + 1
        for h, pr in r.get("fail_hour_prs") or []:
            prs.setdefault(h, set()).add(int(pr))
    return [
        {
            "org": org,
            "qa_namespace": tracker_namespace,
            "hour": h,
            "day": day,
            "failing_tests": n,
            "failing_prs": len(prs.get(h, ())),
            "partial": 1 if partial else 0,
            "created_at": created_at,
            "version": version,
        }
        for h, n in sorted(tests.items())
    ]


# --- the reader's fold --------------------------------------------------------------


@dataclass(frozen=True)
class Coverage:
    """What the roll-up covers of a window: the days read from it, the
    instant the live read starts at, and the days missing before it."""

    since: datetime
    live_since: datetime
    days: list[date]
    missing: list[date]
    partial_day: date | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": bool(self.days),
            "days": len(self.days),
            "from": self.days[0].isoformat() if self.days else None,
            "to": self.days[-1].isoformat() if self.days else None,
            "live_since": self.live_since.isoformat(),
            "missing_days": [d.isoformat() for d in self.missing][:31],
            "partial_day": self.partial_day.isoformat() if self.partial_day else None,
        }


def coverage(rolled: dict[date, tuple[bool, datetime]], since: datetime, now: datetime) -> Coverage:
    """Walk the window's days from `since`: a final day covers itself, a
    partial day covers up to `rolled_until` and ends the walk, a missing day
    ends it — the live read starts where the walk stopped. Nothing rolled →
    live from `since` (the unrolled read)."""
    first = since.date()
    days: list[date] = []
    missing: list[date] = []
    live_since = datetime(first.year, first.month, first.day)
    partial_day: date | None = None
    day = first
    while day <= now.date():
        start, end = day_bounds(day)
        entry = rolled.get(day)
        if entry is None:
            missing.append(day)
            live_since = start
            break
        partial, until = entry
        days.append(day)
        if partial:
            partial_day = day
            live_since = max(start, min(until, end))
            break
        live_since = end
        day += timedelta(days=1)
    if not days:
        live_since = since
    return Coverage(
        since=since, live_since=live_since, days=days, missing=missing, partial_day=partial_day
    )


def fold_window(
    daily_rows: list[dict[str, Any]], live_row: dict[str, Any] | None, *, max_hours: int
) -> dict[str, Any] | None:
    """The one history row `test_signal.features` reads, from the roll-up
    rows of the window plus the live row of its tail. `prs_total` sums the
    days' distinct PR heads (an upper bound: a head built on two days counts
    twice); every other count is exact up to a sequence cut at midnight.
    None when nothing ran."""
    live = dict(live_row or {})
    runs = sum(int(r.get("runs") or 0) for r in daily_rows) + int(live.get("runs_total") or 0)
    if runs == 0:
        return None
    fail_prs: set[int] = set()
    hours: set[datetime] = set()
    pairs: set[tuple[datetime, int]] = set()
    last_true, last_head = EPOCH, ""
    for r in daily_rows:
        fail_prs.update(int(p) for p in (r.get("fail_prs") or []))
        hours.update(_dt(h) for h in (r.get("fail_hours") or []))
        pairs.update(_pairs(r.get("fail_hour_prs")))
        lt = _dt(r.get("last_true_fail"))
        if lt > last_true:
            last_true, last_head = lt, str(r.get("last_true_fail_head") or "")
    fail_prs.update(int(p) for p in (live.get("fail_prs") or []))
    hours.update(_dt(h) for h in (live.get("fail_hours_all") or []))
    pairs.update(_pairs(live.get("fail_hour_prs")))
    live_true = _dt(live.get("last_true_fail"))
    if live_true > last_true:
        last_true, last_head = live_true, str(live.get("last_true_fail_head") or "")
    firsts = [_dt(r.get("first_run_at")) for r in daily_rows] + [_dt(live.get("first_run_at"))]
    firsts = [f for f in firsts if f > EPOCH]

    def total(day_key: str, live_key: str) -> int:
        return sum(int(r.get(day_key) or 0) for r in daily_rows) + int(live.get(live_key) or 0)

    return {
        "runs_total": runs,
        "fails_total": total("fails", "fails_total"),
        "prs_total": total("prs", "prs_total"),
        "failing_prs": len(fail_prs),
        "branch_fails": total("branch_fails", "branch_fails"),
        "last_true_fail": last_true,
        "last_true_fail_head": last_head,
        "last_fail_at": max(
            [_dt(r.get("last_fail_at")) for r in daily_rows] + [_dt(live.get("last_fail_at"))]
        ),
        "first_run_at": min(firsts) if firsts else EPOCH,
        "flips_total": total("flips", "flips_total"),
        "steps_total": total("steps", "steps_total"),
        "rerun_flips_total": total("same_commit_flips", "rerun_flips_total"),
        "fail_hours_all": sorted(hours)[-max(1, int(max_hours)) :],
        "fail_hour_prs": sorted(pairs),
    }


# --- the two features ---------------------------------------------------------------


def streak(
    daily_rows: list[dict[str, Any]], live_row: dict[str, Any] | None, pr: int
) -> dict[str, Any]:
    """The previous-outcome block for one PR head: the newest day it failed
    this test on, its failures, the trailing failure run of its failing check
    sequence that day, whether that sequence's latest run failed, and how
    many days of the window it failed on. `known` is False when the head
    never failed the test in the window — a pass, or it never ran it; the
    roll-up keeps no per-head record of passing runs."""
    out: dict[str, Any] = {
        "pr_number": int(pr),
        "known": False,
        "fail_days": 0,
        "last_fail_day": None,
        "fails_on_day": 0,
        "tail_streak": 0,
        "last_outcome": "unknown",
        "subsequent_failure": False,
        "source": "",
    }
    if pr <= 0:
        return out
    newest: tuple[date, int, int, int, str] | None = None
    fail_days = 0
    for r in daily_rows:
        prs = [int(p) for p in (r.get("fail_prs") or [])]
        if pr not in prs:
            continue
        i = prs.index(pr)
        fail_days += 1
        d = r.get("day")
        day = d if isinstance(d, date) else date.fromisoformat(str(d))
        row = (
            day,
            int((r.get("fail_pr_fails") or [0] * (i + 1))[i]),
            int((r.get("fail_pr_tail") or [0] * (i + 1))[i]),
            int((r.get("fail_pr_last") or [0] * (i + 1))[i]),
            "rollup",
        )
        if newest is None or row[0] > newest[0]:
            newest = row
    live = live_row or {}
    live_prs = [int(p) for p in (live.get("fail_prs") or [])]
    if pr in live_prs:
        i = live_prs.index(pr)
        fail_days += 1
        last = _dt(live.get("last_fail_at"))
        newest = (
            last.date() if last > EPOCH else date.max,
            int((live.get("fail_pr_fails") or [0] * (i + 1))[i]),
            int((live.get("fail_pr_tail") or [0] * (i + 1))[i]),
            int((live.get("fail_pr_last") or [0] * (i + 1))[i]),
            "live",
        )
    if newest is None:
        return out
    day, fails, tail, last_failed, source = newest
    out.update(
        known=True,
        fail_days=fail_days,
        last_fail_day=day.isoformat() if day != date.max else None,
        fails_on_day=fails,
        tail_streak=tail,
        last_outcome="fail" if last_failed else "pass",
        subsequent_failure=bool(last_failed),
        source=source,
    )
    return out


def smell_flags(texts: list[str]) -> dict[str, Any]:
    """Count the flaky-test smell tokens per category over the test's own
    source; `flags` lists the categories seen, `loc` the lines read."""
    counts: dict[str, int] = dict.fromkeys(SMELL_CATEGORIES, 0)
    loc = 0
    for text in texts:
        loc += text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        for cat, pat in SMELL_PATTERNS.items():
            counts[cat] += len(pat.findall(text))
    flags = [c for c in SMELL_CATEGORIES if counts[c] > 0]
    return {"available": bool(texts), "loc": loc, "counts": counts, "flags": flags}


def prior(
    feat: dict[str, Any],
    verdict: str,
    *,
    flip_rate: float,
    streak_block: dict[str, Any],
    smells: dict[str, Any],
    churn: dict[str, Any],
) -> dict[str, Any]:
    """The CI prior: what to expect of the next run on this PR head, and the
    flake indicators behind it — counted, not weighted (no labels to
    calibrate on; a weighted smell model needs coverage loom lacks). A red
    previous outcome says the next run is red unless the test reads flaky,
    in which case the rule does not apply."""
    indicators: list[str] = []
    if int(feat.get("rerun_flips") or 0) >= 1:
        indicators.append("history_rerun_flip")
    if float(feat.get("flip_rate") or 0.0) >= flip_rate:
        indicators.append("history_flip_rate")
    if int(feat.get("failing_prs") or 0) >= 3 and float(feat.get("fail_rate") or 0.0) < 0.5:
        indicators.append("history_spread_prs")
    indicators.extend(f"smell_{c}" for c in (smells.get("flags") or []))
    if churn.get("available") and int(churn.get("home_prs") or 0) > 0:
        indicators.append("churn_home")
    if churn.get("available") and int(churn.get("source_prs") or 0) > 0:
        indicators.append("churn_sources")
    n = len(indicators)
    level = "high" if n >= 3 else ("medium" if n == 2 else ("low" if n == 1 else "none"))
    if not streak_block.get("known"):
        nxt = "unknown"
    elif streak_block.get("subsequent_failure") and verdict != "flaky":
        nxt = "likely_fail"
    elif streak_block.get("subsequent_failure"):
        nxt = "uncertain_flaky"
    else:
        nxt = "likely_pass"
    return {"next_run_on_pr": nxt, "flake_indicators": indicators, "flake_prior": level}


__all__ = [
    "Coverage",
    "DayPlan",
    "EPOCH",
    "RemoteChecks",
    "SMELL_CATEGORIES",
    "SMELL_PATTERNS",
    "coverage",
    "day_bounds",
    "fold_window",
    "hour_rows",
    "merge_day",
    "parse_remote_clause",
    "per_pr_arrays",
    "plan_days",
    "prior",
    "smell_flags",
    "streak",
]
