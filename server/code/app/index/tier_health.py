"""Writes an entry's tier-health record (`server.code.domain.index.tier_health`).

A failure appends to the run of failures; a success after a failure clears it. A
success over a record that is already ok writes nothing, so a healthy entry costs one
state read per tier per pass (two for a current Go entry, which reads its record
first). A tier the entry no longer runs drops its record (`forget`), so a failure it
left does not read as failing for ever. Recording never fails the pass it describes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.code.domain.index import tier_health
from server.core.datetimes import utc_now

if TYPE_CHECKING:
    from server.code.domain.index.state import IndexState
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_index.tier_health")


def error_text(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


async def record(
    repo: CodeRepo, org: str, ns: str, tier: str, error: BaseException | str | None = None
) -> tier_health.TierHealth | None:
    """Record one pass of `tier` on the entry: ok when `error` is None, else failed.
    Returns the record written, or None when nothing was written."""
    key = tier_health.state_key(tier)
    try:
        prev = tier_health.parse(tier, await repo.get_state(org, ns, key))
        if error is None:
            if prev is None or not prev.failing:
                return None
            new = tier_health.recovered(tier, utc_now())
        else:
            new = tier_health.failed(prev, tier, error_text(error), utc_now())
        await repo.set_state(org, ns, key, new.to_json(), detail=new.kind)
    except Exception as e:  # noqa: BLE001 — a health record never fails the pass it describes
        logger.warning("tier health %s/%s %s not recorded: %s", org, ns, tier, e)
        return None
    if new.failing:
        logger.warning(
            "tier %s of %s/%s failing (%s, %d passes since %s): %s",
            tier,
            org,
            ns,
            new.kind,
            new.failed_passes,
            new.failing_since,
            new.fix,
        )
    return new


async def forget(repo: CodeRepo, org: str, ns: str, tiers: tuple[str, ...]) -> None:
    """Drop the records of `tiers` on an entry that no longer runs them. One state read
    per tier; a write only where a record is left."""
    for tier in tiers:
        key = tier_health.state_key(tier)
        try:
            if await repo.get_state(org, ns, key):
                await repo.set_state(org, ns, key, "", detail="not run")
        except Exception as e:  # noqa: BLE001 — a health record never fails the pass it describes
            logger.warning("tier health %s/%s %s not dropped: %s", org, ns, tier, e)


@dataclass(frozen=True)
class FailingTier:
    """One failing tier of one entry, as the health problem reads it."""

    org: str
    namespace: str
    health: tier_health.TierHealth
    lag_commits: int = -1


# The failing tiers of every entry, as this process last read them: the health probe
# reads this snapshot and never ClickHouse. None until the first read.
_FAILING: tuple[FailingTier, ...] | None = None


async def refresh_failing(ch: Any | None = None) -> None:
    """Re-read the tier-health records of every configured entry and, for the failing
    ones, how far behind the entry's published status counts each (the count `as_of`
    reads: -1 once the snapshot no longer matches the clone tip and the watermark). Two
    small reads; called on the code_repos refresh timer. A record of an entry no longer
    configured (removed from the config, or retired), or kept while code intelligence is
    off, is not a problem: nothing runs it.
    Never raises: a failed read keeps the last snapshot."""
    global _FAILING
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo
    from server.settings import settings

    if not settings.code_intel_enabled:
        _FAILING = ()  # nothing runs a tier: no record is a live failure
        return
    try:
        store = CodeRepo(ch)
        configured = {(c.org, c.namespace): c for c in configured_repos()}
        failing: list[tuple[str, str, tier_health.TierHealth]] = []
        for row in await store.state_rows_with_prefix(tier_health.STATE_PREFIX):
            org, ns = str(row["org"]), str(row["namespace"])
            if (org, ns) not in configured:
                continue
            tier = str(row["state_key"])[len(tier_health.STATE_PREFIX) :]
            health = tier_health.parse(tier, str(row.get("state_value") or ""))
            if health is not None and health.failing:
                failing.append((org, ns, health))
        states = await store.all_state_many(sorted({(o, n) for o, n, _ in failing}))
        out = [
            FailingTier(
                org,
                ns,
                health,
                _published_lag(
                    states.get((org, ns)) or {}, configured[(org, ns)].git_branch, health.tier
                ),
            )
            for org, ns, health in failing
        ]
        _FAILING = tuple(sorted(out, key=lambda f: (f.org, f.namespace, f.health.tier)))
    except Exception as e:  # noqa: BLE001 — the health signal keeps its last reading
        logger.warning("tier health: failing tiers not re-read: %s", e)


def _published_lag(state: IndexState, branch: str, tier: str) -> int:
    """How far the watermark `tier` advances trails the fetched tip, as the entry's
    published status counts it; -1 for the fetch tier or when unknown."""
    from server.code.app.index_status import published_clone_head, published_lags

    key = tier_health.WATERMARK_FOR_TIER.get(tier, "")
    mark = str((state.get(key) or {}).get("value") or "") if key else ""
    if not mark:
        return -1
    return published_lags(state, {key: mark}, published_clone_head(state, branch))[key]


def reset_for_tests() -> None:
    global _FAILING
    _FAILING = None


def problem_rows(now: datetime | None = None) -> tuple[dict[str, Any], ...] | None:
    """The failing tiers past the warn threshold, as the `code_tier_failing` health
    problem reads them (plain rows: the problem collector imports no code plane), worst
    first. None until this process first read the records."""
    from server.settings import settings

    snap = _FAILING
    if snap is None:
        return None
    at = now or utc_now()
    rows = []
    for f in snap:
        level = tier_health.severity(
            f.health,
            f.lag_commits,
            at,
            warn_passes=settings.code_tier_failing_warn_passes,
            warn_hours=settings.code_tier_failing_warn_hours,
            error_lag_commits=settings.code_tier_failing_error_lag_commits,
        )
        if level:
            rows.append(
                {
                    "org": f.org,
                    "namespace": f.namespace,
                    "tier": f.health.tier,
                    "kind": f.health.kind,
                    "since": f.health.failing_since,
                    "failed_passes": f.health.failed_passes,
                    "lag_commits": f.lag_commits,
                    "fix": f.health.fix,
                    "severity": level,
                }
            )
    return tuple(sorted(rows, key=lambda r: (r["severity"] != "error", r["org"], r["namespace"])))
