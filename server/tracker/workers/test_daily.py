"""The per-test daily roll-up of the public CI history.

Every pass plans the days to roll (`test_daily.plan_days`): today up to the
current hour as a partial day, yesterday final once the settle lag has
passed, and the missing days of the backfill window, newest first, at most
`tracker_test_daily_max_days_per_pass` of them. A day costs two statements at the
remote through the tracker source layer — the fold over every test that ran and
the sequences of every test that failed — and becomes one row per test in
`loom.qa_test_daily`, one row per failing hour in `loom.qa_fail_hours`, and
one ledger row in `loom.qa_rollup_days` that says how far the day is rolled
and what it cost. The remote meters the shared `play` user at 100
statements of one shape per hour, so the pass paces its
statements and stops at the first `TrackerUnavailable` (quota, budget, breaker):
the next pass picks up where it stopped; nothing is retried inside a pass.

Only a tracker namespace that serves the public repository has a checks history
(`TrackerSource._admit_checks`); the worker rolls each configured one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from server.workers.first_pass import OwnedTable
from server.workers.pass_context import note_failure, set_namespace
from server.workers.status import record_pass

logger = logging.getLogger("loom.workers.tracker_test_daily")

# The tables this pass fills. A configured namespace with no
# rolled day in the ledger (written last, so a half-rolled day is not one)
# or no test row runs at boot instead of after the 761 s stagger.
OWNED_TABLES = (
    OwnedTable("loom.qa_rollup_days", namespace_column="qa_namespace"),
    OwnedTable("loom.qa_test_daily", namespace_column="qa_namespace"),
)


@dataclass
class TrackerTestDailyResult:
    namespaces: int = 0
    days_planned: int = 0  # Days the pass set out to roll
    days_rolled: int = 0
    rows: int = 0
    hour_rows: int = 0  # tracker_fail_hours rows written
    statements: int = 0
    remote_ms: int = 0
    stopped: str = ""
    errors: list[str] = field(default_factory=list)


def _repo() -> Any:
    from server.tracker.store.daily import TrackerDailyRepo

    return TrackerDailyRepo()


def _source(org: str, ns: str) -> Any:
    from server.tracker.app.source import tracker_source

    return tracker_source(org, ns)


def _namespaces() -> list[tuple[str, str]]:
    """(org, namespace) of every configured tracker namespace that serves the
    public repository — the only ones with a checks history."""
    from server.tracker.domain.namespaces import configured_namespaces
    from server.tracker.store.source import checks_config

    out: list[tuple[str, str]] = []
    for cfg in configured_namespaces().values():
        frm, repo = checks_config(cfg.org, cfg.namespace)
        if frm and (not repo or repo in cfg.repos):
            out.append((cfg.org, cfg.namespace))
    return sorted(set(out))


def due_scope() -> list[tuple[str, str]]:
    """The namespaces whose owned tables decide the first pass
    — the same set the pass rolls."""
    return _namespaces()


async def run_pass() -> TrackerTestDailyResult:
    result = TrackerTestDailyResult()
    with record_pass("tracker_test_daily") as rec:
        await _run(result)
        rec.outcome = {
            # The row's counters first, under their own names
            # — days planned / rolled, and every row the pass wrote (tests,
            # failing hours, ledger)
            "items_planned": result.days_planned,
            "items_done": result.days_rolled,
            "rows_written": result.rows + result.hour_rows + result.days_rolled,
            "namespaces": result.namespaces,
            "days_rolled": result.days_rolled,
            "rows": result.rows,
            "hour_rows": result.hour_rows,
            "statements": result.statements,
            "remote_ms": result.remote_ms,
            "stopped": result.stopped,
            "errors": len(result.errors),
        }
    return result


async def _run(result: TrackerTestDailyResult) -> None:
    from server.settings import settings
    from server.tracker.domain import sources

    if not sources.credential_configured():
        result.stopped = "qa credential not configured"
        return
    repo = _repo()
    now = datetime.now(UTC).replace(tzinfo=None)
    for org, ns in _namespaces():
        result.namespaces += 1
        set_namespace(ns)  # Where a cut pass stopped
        try:
            await roll_namespace(repo, org, ns, now, result, settings=settings)
        except Exception as e:  # noqa: BLE001 — one namespace's failure never hides another's
            result.errors.append(f"{org}/{ns}: {type(e).__name__}: {e}")
            logger.warning("tracker_test_daily %s/%s failed: %s", org, ns, e)
            note_failure(e, ns)


async def roll_namespace(
    repo: Any,
    org: str,
    ns: str,
    now: datetime,
    result: TrackerTestDailyResult,
    *,
    settings: Any,
) -> None:
    from server.tracker.domain import test_daily as td
    from server.tracker.domain.errors import TrackerUnavailable

    backfill = max(1, int(settings.tracker_test_daily_backfill_days))
    since_day = (now - timedelta(days=backfill)).date()
    rolled = await repo.rolled_days(org, ns, since_day, now.date())
    plan = td.plan_days(
        rolled,
        now,
        backfill_days=backfill,
        lag_hours=float(settings.tracker_test_daily_lag_hours),
        max_days=max(1, int(settings.tracker_test_daily_max_days_per_pass)),
    )
    pace = max(0.0, float(settings.tracker_test_daily_pace_s))
    result.days_planned += len(plan)
    src = _source(org, ns)
    for i, day in enumerate(plan):
        if i and pace:
            await asyncio.sleep(pace)
        try:
            await roll_day(repo, src, org, ns, day, result, pace=pace)
        except TrackerUnavailable as e:
            # the quota, the budget or the breaker: the rest waits for the next pass.
            # TrackerUnavailable's messages are Loom's own (the source cuts driver text),
            # and `stopped` reaches only the admin worker status (`ops.health` keeps an
            # outcome's numbers).
            result.stopped = f"{org}/{ns} {day.day.isoformat()}: {str(e)[:200]}"
            logger.info("tracker_test_daily stopped at %s: %s", day.day, e)
            return


async def roll_day(
    repo: Any,
    src: Any,
    org: str,
    ns: str,
    day: Any,
    result: TrackerTestDailyResult,
    *,
    pace: float,
) -> None:
    """Two statements, one merge, three inserts; the ledger row last so a
    half-written day reads as not rolled."""
    from server.tracker.domain import test_daily as td
    from server.tracker.store.daily import version_now
    from server.tracker.store.source import checks_source_label

    t0 = time.monotonic()
    fold = await src.test_day_fold(org, ns, since=day.since, until=day.until)
    result.statements += 1
    if pace:
        await asyncio.sleep(pace)
    seqs = await src.test_day_sequences(org, ns, since=day.since, until=day.until)
    result.statements += 1
    remote_ms = int((time.monotonic() - t0) * 1000)
    stamp = datetime.now(UTC).replace(tzinfo=None)
    version = version_now()
    rows = td.merge_day(
        fold,
        seqs,
        org=org,
        tracker_namespace=ns,
        day=day.day,
        partial=day.partial,
        rolled_until=day.until,
        version=version,
        rolled_at=stamp,
    )
    hours = td.hour_rows(
        rows,
        org=org,
        tracker_namespace=ns,
        day=day.day,
        partial=day.partial,
        version=version,
        created_at=stamp,
    )
    await repo.insert_daily(rows)
    await repo.insert_hours(hours)
    await repo.insert_rolled_day(
        {
            "org": org,
            "qa_namespace": ns,
            "day": day.day,
            "partial": 1 if day.partial else 0,
            "rolled_until": day.until,
            "tests": len(rows),
            "failing_tests": sum(1 for r in rows if int(r["fails"]) > 0),
            "runs": sum(int(r["runs"]) for r in rows),
            "statements": 2,
            "remote_ms": remote_ms,
            "source": checks_source_label(org, ns)[:200],
            "rolled_at": stamp,
            "created_at": stamp,
            "version": version,
        }
    )
    result.days_rolled += 1
    result.rows += len(rows)
    result.hour_rows += len(hours)
    result.remote_ms += remote_ms
    logger.info(
        "tracker_test_daily %s/%s %s%s: %d tests, %d failing hours, remote %d ms",
        org,
        ns,
        day.day,
        " (partial)" if day.partial else "",
        len(rows),
        len(hours),
        remote_ms,
    )


__all__ = ["TrackerTestDailyResult", "roll_day", "roll_namespace", "run_pass"]
