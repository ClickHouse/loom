"""tracker.test_signal and the legs tracker.flaky shares with it: the daily
roll-up, the history and co-failure reads, the storm and the churn of the
test's code, joined into one verdict.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from server.code.app.reads import CodeIndexReads
from server.settings import settings
from server.tracker.api.models import (
    TrackerTestSignalInput,
    TrackerTestSignalOutput,
)
from server.tracker.app import common
from server.tracker.app import source as tsource
from server.tracker.domain import test_daily as td
from server.tracker.domain import test_signal as ts
from server.tracker.domain.errors import TrackerUnavailable

logger = logging.getLogger(__name__)


def _thresholds() -> ts.Thresholds:
    return ts.Thresholds(
        min_runs=max(1, int(settings.tracker_test_signal_min_runs)),
        flip_rate=float(settings.tracker_test_signal_flip_rate),
        cofail_k=max(1, int(settings.tracker_test_signal_cofail_k)),
        cofail_multiple=float(settings.tracker_test_signal_cofail_multiple),
        cofail_prs=max(1, int(settings.tracker_test_signal_cofail_prs)),
        cofail_share=float(settings.tracker_test_signal_cofail_share),
    )


def _code_repo() -> CodeIndexReads:
    from server.code.app.reads import code_index

    return code_index()


async def _code_namespace_authorized(org: str, code_namespace: str) -> tuple[str, str, str]:
    """(org, namespace, refusal): the ledger namespace churn is read from —
    the caller's or the deployment's — as the current principal (the
    transport authorized the tracker namespace, not this one)."""
    from server.code.domain.index.repos import RegistryUnread, default_code_namespace
    from server.core.errors import ValidationError

    code_org = str(settings.code_org or org)
    try:
        ns = code_namespace or default_code_namespace(code_org)
    except (RegistryUnread, ValidationError) as e:
        return code_org, "", str(e)
    if not ns:
        return code_org, "", "no code namespace configured"
    try:
        from server.auth.authz import row_in_scope
        from server.operations.base import Role

        # A code namespace the caller named holds part of the answer; the
        # deployment's default only enriches it.
        named = bool(code_namespace)
        if not await row_in_scope(code_org, ns, Role.VIEWER, hides_answer=named):
            return code_org, ns, "unauthorized"
    except Exception as e:  # noqa: BLE001 — closed
        return code_org, ns, f"authorization failed: {type(e).__name__}"
    return code_org, ns, ""


async def churn_leg(org: str, code_namespace: str, test_name: str, *, days: int) -> dict[str, Any]:
    """Loom's leg: merged PRs of the ledger that touched the test's home
    file(s) (by naming convention, resolved against the indexed files) and
    the sources the test map ties to it, in the last `days` days."""
    code_org, ns, refusal = await _code_namespace_authorized(org, code_namespace)
    if refusal:
        return ts.churn_unavailable(refusal, days=days)
    stems, prefix = ts.home_of(test_name)
    if not stems and not prefix:
        return {
            **ts.churn_unavailable("no home convention for this test name", days=days),
            "code_namespace": ns,
        }
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    repo = _code_repo()
    try:
        home_paths: list[str] = []
        if stems:
            rows = await repo.paths_by_stem(code_org, ns, stems)
            home_paths = [str(r["path"]) for r in rows if ts.is_test_home_path(str(r["path"]))]
        source_paths = await repo.sources_of_tests(code_org, ns, home_paths) if home_paths else []
        home_prs = (
            await repo.pr_churn_touching(code_org, ns, paths=home_paths, prefix=prefix, since=since)
            if home_paths or prefix
            else []
        )
        source_prs = (
            await repo.pr_churn_touching(code_org, ns, paths=source_paths, since=since)
            if source_paths
            else []
        )
    except Exception as e:  # noqa: BLE001 — the ledger leg never fails the verdict
        logger.info("tracker.test_signal churn leg on %s/%s failed: %s", code_org, ns, e)
        return {
            **ts.churn_unavailable(f"ledger read failed: {type(e).__name__}", days=days),
            "code_namespace": ns,
        }
    if prefix and not home_paths:
        home_paths = [prefix]
    out = ts.churn_summary(home_paths, home_prs, source_paths, source_prs, days=days)
    out["code_namespace"] = ns
    return out


def _daily_repo() -> Any:
    from server.tracker.store.daily import TrackerDailyRepo

    return TrackerDailyRepo()


async def _rollup_leg(
    org: str,
    namespace: str,
    name: str,
    since: datetime,
    now: datetime,
) -> tuple[list[dict[str, Any]], Any, dict[str, Any]]:
    """The roll-up rows of the window and what they cover — (rows, coverage, the `rollup`
    block). Nothing rolled or a failed read: no rows, coverage None, and the live read takes
    the whole window."""
    repo = _daily_repo()
    try:
        rolled = await repo.rolled_days(org, namespace, since.date(), now.date())
        cov = td.coverage(rolled, since, now)
        rows = (
            await repo.window(org, namespace, name, cov.days[0], cov.days[-1]) if cov.days else []
        )
    except Exception as e:  # noqa: BLE001 — the roll-up never fails the read
        logger.info("tracker.test_signal roll-up read on %s failed: %s", namespace, e)
        return [], None, {"available": False, "reason": f"roll-up read failed: {type(e).__name__}"}
    if not cov.days:
        return [], None, {**cov.as_dict(), "reason": "no day of the window is rolled"}
    return rows, cov, cov.as_dict()


async def _history_leg(
    src: Any,
    org: str,
    namespace: str,
    name: str,
    since: datetime,
    now: datetime,
    rows: list[dict[str, Any]],
    cov: Any,
    rollup: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, datetime]:
    """(history row, live row, live_since): the roll-up rows folded with the
    live tail the roll-up does not cover. Raises TrackerUnavailable only when
    there is no roll-up to fall back on."""
    live_since = cov.live_since if cov is not None else since
    live_row: dict[str, Any] | None = None
    if live_since < now:
        try:
            live_row = await src.test_history(
                org, namespace, name, since=live_since, until=now + timedelta(hours=1)
            )
        except TrackerUnavailable as e:
            if not rows:
                raise
            common._unavailable("tracker.test_signal/live_tail", namespace, e)
            rollup["live_unavailable"] = str(e)[:200]
    if not rows:
        return live_row, live_row, live_since
    return td.fold_window(rows, live_row, max_hours=ts.MAX_FAIL_HOURS), live_row, live_since


async def _baseline(
    src: Any,
    repo: Any,
    org: str,
    namespace: str,
    since: datetime,
    live_since: datetime,
    has_rollup: bool,
    out: dict[str, Any],
) -> int:
    """The window's median failing tests per hour: from the roll-up hours
    when it holds any, else the live statement."""
    median = 0
    if has_rollup:
        try:
            median = await repo.hour_median(org, namespace, since, live_since)
        except Exception as e:  # noqa: BLE001 — falls back to the live read
            out.update(rollup_baseline_unavailable=f"{type(e).__name__}")
    if median == 0:
        try:
            median = await src.failing_tests_per_hour_median(org, namespace, since=since)
            out.update(baseline_source="live")
        except TrackerUnavailable as e:
            common._unavailable("tracker.test_signal/baseline", namespace, e)
            out.update(baseline_unavailable=str(e)[:200])
    else:
        out.update(baseline_source="rollup")
    return median


async def _storm_leg(
    src: Any,
    org: str,
    namespace: str,
    name: str,
    feat: dict[str, Any],
    th: ts.Thresholds,
    since: datetime,
    live_since: datetime,
    has_rollup: bool,
) -> dict[str, Any]:
    """The co-failure leg: the window's per-hour baseline, then the other
    failing tests in this test's failing hours — from the roll-up's hour
    table for the hours it covers, live for the tail — against
    K = storm_k(baseline). The storm rule is the same as the live read's."""
    out: dict[str, Any] = {}
    repo = _daily_repo()
    median = await _baseline(src, repo, org, namespace, since, live_since, has_rollup, out)
    k = th.storm_k(median)
    out.update(median_failing_tests_per_hour=median, k=k)
    hours = [datetime.fromisoformat(h) for h in feat["fail_hours"]]
    rolled_hours = [h for h in hours if h < live_since]
    live_hours = [h for h in hours if h >= live_since]
    hour_rows: list[dict[str, Any]] = []
    if rolled_hours and has_rollup:
        try:
            hour_rows.extend(
                {
                    "h": r["hour"],
                    "other_tests": max(0, int(r["failing_tests"]) - 1),
                    "other_prs": int(r["failing_prs"]),
                }
                for r in await repo.hour_stats(org, namespace, rolled_hours)
            )
        except Exception as e:  # noqa: BLE001 — falls back to the live read
            out.update(rollup_hours_unavailable=f"{type(e).__name__}")
            live_hours = hours
    elif rolled_hours:
        live_hours = hours
    if live_hours:
        try:
            hour_rows.extend(await src.co_failures_in_hours(org, namespace, name, live_hours))
        except TrackerUnavailable as e:
            common._unavailable("tracker.test_signal/co_failures", namespace, e)
            out.update(available=False, reason=str(e)[:200])
    n, storm = ts.co_fail_hours(hour_rows, k=k, min_prs=th.cofail_prs)
    out.update(
        storm_hours=n,
        hours=storm,
        share=round(n / len(hours), 3) if hours else 0.0,
        rollup_hours=len(rolled_hours),
        live_hours=len(live_hours),
    )
    return out


def _home_for_smells(name: str, churn: dict[str, Any]) -> list[str]:
    """The test's source file(s): the churn leg's resolved home paths, or the
    integration test's `test.py` when the home is a directory."""
    paths = [p for p in (churn.get("home_paths") or []) if not p.endswith("/")]
    if paths:
        return paths[:4]
    _stems, prefix = ts.home_of(name)
    return [f"{prefix}test.py"] if prefix else []


async def smell_leg(
    org: str, code_namespace: str, name: str, churn: dict[str, Any]
) -> dict[str, Any]:
    """Static flakiness smells (test-code patterns that predict flaky tests) counted over the
    chunks of the test's own source in the code plane (read as the current principal)."""
    code_org, ns, refusal = await _code_namespace_authorized(org, code_namespace)
    if refusal:
        return {"available": False, "reason": refusal, "paths": []}
    paths = _home_for_smells(name, churn)
    if not paths:
        return {"available": False, "reason": "no source path for this test name", "paths": []}
    try:
        from server.embedding.embedder import get_embedder

        model = get_embedder().model_name
        per_path = max(1, int(settings.tracker_test_signal_smell_chunks))
        rows = await _code_repo().chunks_for_paths(code_org, ns, model, paths, per_path=per_path)
    except Exception as e:  # noqa: BLE001 — the smell leg never fails the verdict
        logger.info("tracker.test_signal smell leg on %s/%s failed: %s", code_org, ns, e)
        return {
            "available": False,
            "reason": f"chunk read failed: {type(e).__name__}",
            "paths": paths,
        }
    out = td.smell_flags([str(r.get("content") or "") for r in rows])
    out.update(paths=paths, chunks=len(rows), code_namespace=ns)
    if not rows:
        out.update(available=False, reason="no indexed chunk of the test's source")
    return out


def _unavailable_output(
    name: str,
    days: int,
    since: datetime,
    th: ts.Thresholds,
    e: Exception,
    t0: float,
    rollup: dict[str, Any],
) -> TrackerTestSignalOutput:
    return TrackerTestSignalOutput(
        test_name=name,
        verdict=ts.UNAVAILABLE,
        window_days=days,
        since=since.isoformat(),
        thresholds=th.as_dict(),
        tracker_unavailable=True,
        unavailable_reason=str(e)[:300],
        timings_ms={"history": int((time.monotonic() - t0) * 1000)},
        rollup=rollup,
    )


async def _name_candidates(
    org: str, namespace: str, name: str, since: datetime, now: datetime
) -> dict[str, Any] | None:
    """For a name with no run in the window, the rolled tests whose names start with it (a
    key range) or, when none does, contain it — `ts.name_candidates`'s answer. None when
    nothing matches, or the read fails or runs past `tracker_test_candidates_timeout_s`
    (the `no_signal` answer stands)."""
    needle = ts.name_needle(name)
    if len(needle) < 3:
        return None
    repo = _daily_repo()

    async def read() -> dict[str, Any] | None:
        for prefix in (True, False):
            rows = await repo.test_names_like(
                org,
                namespace,
                needle,
                since.date(),
                now.date(),
                prefix=prefix,
                limit=ts.MAX_CANDIDATES,
            )
            if rows:
                return ts.name_candidates(name, needle, rows, prefix=prefix)
        return None

    try:
        return await asyncio.wait_for(read(), timeout=settings.tracker_test_candidates_timeout_s)
    except Exception as e:  # noqa: BLE001 — the candidate read never fails the answer
        logger.info("tracker.test_signal candidates on %s failed: %s", namespace, e)
        return None


def _unknown_test_output(
    name: str,
    days: int,
    since: datetime,
    th: ts.Thresholds,
    named: dict[str, Any],
    timings: dict[str, int],
    rollup: dict[str, Any],
) -> TrackerTestSignalOutput:
    return TrackerTestSignalOutput(
        test_name=name,
        verdict=ts.UNKNOWN_TEST,
        why=[named["why"]],
        window_days=days,
        since=since.isoformat(),
        thresholds=th.as_dict(),
        candidates=named["candidates"],
        candidate_matches=int(named["matches"]),
        timings_ms=timings,
        rollup=rollup,
    )


async def test_signal_for(
    org: str,
    namespace: str,
    test_name: str,
    *,
    window_days: int = 0,
    code_namespace: str = "",
    include_churn: bool = True,
    pr_number: int = 0,
    include_smells: bool = True,
    resolved_from: str = "",
) -> TrackerTestSignalOutput:
    from server.tracker.store.source import checks_source_label

    name = test_name.strip()
    days = int(window_days or 0) or max(1, int(settings.tracker_test_signal_window_days))
    # hour-truncated so every call in the hour shares the cached statements
    now = datetime.now(UTC).replace(tzinfo=None)
    now_hour = now.replace(minute=0, second=0, microsecond=0)
    since = now_hour - timedelta(days=days)
    th = _thresholds()
    src = tsource.tracker_source(org, namespace)
    timings: dict[str, int] = {}
    t0 = time.monotonic()
    rows, cov, rollup = await _rollup_leg(org, namespace, name, since, now)
    try:
        row, live_row, live_since = await _history_leg(
            src, org, namespace, name, since, now, rows, cov, rollup
        )
    except TrackerUnavailable as e:
        common._unavailable("tracker.test_signal", namespace, e)
        return _unavailable_output(name, days, since, th, e, t0, rollup)
    timings["history"] = int((time.monotonic() - t0) * 1000)
    if row is None and not resolved_from:
        t_c = time.monotonic()
        named = await _name_candidates(org, namespace, name, since, now)
        timings["candidates"] = int((time.monotonic() - t_c) * 1000)
        if named and named["resolved"]:
            return await test_signal_for(
                org,
                namespace,
                named["resolved"],
                window_days=days,
                code_namespace=code_namespace,
                include_churn=include_churn,
                pr_number=pr_number,
                include_smells=include_smells,
                resolved_from=name,
            )
        if named:
            return _unknown_test_output(name, days, since, th, named, timings, rollup)
    feat = ts.features(row)
    # the roll-up sums each day's distinct PR heads (a head built on two days counts twice)
    feat["prs_kind"] = "head_days" if rows else "heads"
    infra: dict[str, Any] = {
        "k": th.cofail_k,
        "k_multiple": th.cofail_multiple,
        "median_failing_tests_per_hour": None,
        "min_prs": th.cofail_prs,
        "multi_pr_hours": ts.multi_pr_hours(feat, th.cofail_prs),
        "storm_hours": 0,
        "hours": [],
        "share": 0.0,
        "available": True,
    }
    if feat["fail_hours"]:
        t1 = time.monotonic()
        infra.update(
            await _storm_leg(
                src, org, namespace, name, feat, th, since, live_since, cov is not None
            )
        )
        timings["co_failures"] = int((time.monotonic() - t1) * 1000)
    verdict, why, lb = ts.verdict(feat, int(infra["storm_hours"]), th, storm_k=int(infra["k"]))
    unread = _storm_unread(feat, infra, th, verdict)
    churn: dict[str, Any] = {}
    churn_days = max(1, int(settings.tracker_test_signal_churn_days))
    if include_churn:
        t2 = time.monotonic()
        churn = await churn_leg(org, code_namespace, name, days=churn_days)
        timings["churn"] = int((time.monotonic() - t2) * 1000)
    smells: dict[str, Any] = {}
    if include_smells:
        t3 = time.monotonic()
        smells = await smell_leg(org, code_namespace, name, churn)
        timings["smells"] = int((time.monotonic() - t3) * 1000)
    streak = td.streak(rows, live_row, int(pr_number or 0)) if pr_number else {}
    prior = td.prior(
        feat, verdict, flip_rate=th.flip_rate, streak_block=streak, smells=smells, churn=churn
    )
    if unread:
        verdict, why = ts.UNAVAILABLE, [unread, *why]
    if resolved_from:
        why = [
            f"no test named {resolved_from!r}; answered for {name!r}, the only test that matches",
            *why,
        ]
    return TrackerTestSignalOutput(
        test_name=name,
        resolved_from=resolved_from,
        verdict=verdict,
        why=why,
        lower_bound=lb,
        window_days=days,
        since=since.isoformat(),
        features=feat,
        infra=infra,
        churn=churn,
        thresholds=th.as_dict(),
        source=checks_source_label(org, namespace),
        timings_ms=timings,
        rollup=rollup,
        streak=streak,
        smells=smells,
        prior=prior,
        tracker_unavailable=bool(unread),
        unavailable_reason=str(infra.get("reason") or "")[:300] if unread else "",
    )


def _storm_unread(
    feat: dict[str, Any], infra: dict[str, Any], th: ts.Thresholds, verdict: str
) -> str:
    """ "" unless the co-failure read failed and the hours it left unread decide the
    verdict: every unread hour a storm would answer differently than none. Then one
    sentence saying so — the counted storms are a floor, not a zero. Likewise when
    the baseline read failed: K fell to its floor, so the counted storms are a
    ceiling, and a verdict that holds only with them is undecided."""
    if infra.get("baseline_unavailable") and int(infra.get("storm_hours") or 0) > 0:
        fewest, _, _ = ts.verdict(feat, 0, th, storm_k=int(infra["k"]))
        if fewest != verdict:
            return (
                f"the baseline read failed ({str(infra['baseline_unavailable'])[:120]}): "
                f"K is its floor {int(infra['k'])}, so {verdict} holds only if every "
                f"counted storm hour is one; {fewest} if none is — undecided"
            )
    if infra.get("available", True):
        return ""
    most = int(infra["storm_hours"]) + int(infra.get("live_hours") or 0)
    other, _, _ = ts.verdict(feat, most, th, storm_k=int(infra["k"]))
    if other == verdict:
        return ""
    return (
        f"the co-failure read failed ({str(infra.get('reason') or '')[:120]}): "
        f"{verdict} if its hours held no storm, {other} if they did — undecided"
    )


async def handle_tracker_test_signal(input: TrackerTestSignalInput) -> TrackerTestSignalOutput:
    input = await common._bound_input(input)
    return await test_signal_for(
        input.org,
        input.namespace,
        input.test_name,
        window_days=int(input.window_days or 0),
        code_namespace=str(input.code_namespace or ""),
        include_churn=bool(input.include_churn),
        pr_number=int(input.pr_number or 0),
        include_smells=bool(input.include_smells),
    )
