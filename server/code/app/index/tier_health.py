"""Writes an entry's tier-health record (`server.code.domain.index.tier_health`).

A failure appends to the run of failures; a success after a failure clears it. A
success over a record that is already ok writes nothing, so a healthy entry costs one
state read per tier per pass (two for a current Go entry, which reads its record
first). A tier the entry no longer runs drops its record (`forget`), so a failure it
left does not read as failing for ever. Recording never fails the pass it describes.

It also keeps this process's snapshot of the failing tiers (`refresh_failing`, on the
code_repos refresh timer), which the `code_tier_failing` health problem reads
(`problem_rows`) without a ClickHouse read in the health probe.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.code.app.index.gitrepo import GitError
from server.code.app.index.untrusted_files import FileRefused
from server.code.domain.index import tier_health
from server.core.datetimes import utc_now
from server.core.errors import caller_text

if TYPE_CHECKING:
    from server.code.domain.index.state import IndexState
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_index.tier_health")


class ToolOutputError(RuntimeError):
    """A tier failure whose message is the tier's own tool output about the entry (a
    harness exit and its tail), or the tier's own words about that tool (it is missing,
    it timed out): kept in the record for its fix."""


def error_text(error: BaseException | str) -> str:
    """The record's error, which `code.index_status` shows a viewer: a tool's or git's
    output whole (redacted later), a refused file's own words (its sizes and cap), a
    file error with the file it names, any other exception's kind, since its text can
    carry a statement or the server's address. A file's path is absolute: it shows the
    entry's reader where under the code repos root or the work directory the tier read,
    as git's own errors do."""
    if isinstance(error, ToolOutputError | GitError | FileRefused) or (
        isinstance(error, OSError) and error.filename is not None
    ):
        return f"{type(error).__name__}: {error}"
    if isinstance(error, BaseException):
        return caller_text(error)
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
    lag_commits: int
    repo: str  # owner/name
    branch: str
    # restricted by its repository entry or directory row, or unreadable: no admin
    # role reads which entry it is
    restricted: bool


# The failing tiers of every entry, as this process last read them: the health probe
# reads this snapshot and never ClickHouse. None until the first read.
_FAILING: tuple[FailingTier, ...] | None = None


async def refresh_failing(ch: Any | None = None) -> None:
    """Re-read the tier-health records of every configured entry and, for the failing
    ones, how far behind the entry's last published status counted each
    (`_published_lag`) and whether the entry is restricted. Two small reads, and the
    classification once per org a cache period; called on the code_repos refresh timer.
    A record of an entry no longer configured (removed from the config, or retired), or
    kept while code intelligence is off, is not a problem: nothing runs it. Until the
    registry is first read, which entries are configured is unknown, and the snapshot
    stays as it was. Never raises: a failed read keeps the last snapshot."""
    global _FAILING
    from server.code.domain.index.repos import configured_repos
    from server.code.store.index import CodeRepo
    from server.settings import settings

    if not settings.code_intel_enabled:
        _FAILING = ()  # nothing runs a tier: no record is a live failure
        return
    from server.code.app.index import repo_registry

    if not repo_registry.registry_read():
        return  # which entries are configured is unknown: the snapshot stays as it was
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
        out = []
        for org, ns, health in failing:
            cfg = configured[(org, ns)]
            out.append(
                FailingTier(
                    org,
                    ns,
                    health,
                    _published_lag(states.get((org, ns)) or {}, health.tier),
                    cfg.repo,
                    cfg.git_branch,
                    await _restricted(org, ns),
                )
            )
        _FAILING = tuple(sorted(out, key=lambda f: (f.org, f.namespace, f.health.tier)))
    except Exception as e:  # noqa: BLE001 — the health signal keeps its last reading
        logger.warning("tier health: failing tiers not re-read: %s", e)


async def _restricted(org: str, ns: str) -> bool:
    """Whether the entry is restricted; True when its classification cannot be read."""
    from server.auth.directory import ClassificationUnavailable
    from server.identity.app.namespace_classification import is_restricted

    try:
        return await is_restricted(org, ns)
    except ClassificationUnavailable:
        return True


def _published_lag(state: IndexState, tier: str) -> int:
    """How far the watermark `tier` advances trailed the fetched tip when the entry's
    status was last published, while that watermark has not moved since: a lower bound
    of the lag now (the tip only advances), enough for the error threshold. -1 for the
    fetch tier or when unknown."""
    from server.code.app.index_status import STATUS_SNAPSHOT_KEY, published_lags

    key = tier_health.WATERMARK_FOR_TIER.get(tier, "")
    mark = str((state.get(key) or {}).get("value") or "") if key else ""
    if not mark:
        return -1
    try:
        snap = json.loads(str((state.get(STATUS_SNAPSHOT_KEY) or {}).get("value") or "{}"))
    except ValueError:
        return -1
    # The snapshot's own tip: `published_lags` then checks only that the watermark is
    # the one it counted from, not that the tip has not moved since.
    tip = str(snap.get("clone_head") or "") if isinstance(snap, dict) else ""
    return published_lags(state, {key: mark}, tip)[key]


def reset_for_tests() -> None:
    global _FAILING
    _FAILING = None


def problem_rows(now: datetime | None = None) -> tuple[dict[str, Any], ...] | None:
    """The failing tiers past the warn threshold, as the `code_tier_failing` health
    problem reads them (plain rows: the problem collector imports no code plane), errors
    first, then by entry. None until this process first read the records."""
    from server.identity.app.namespace_classification import is_restricted_cached
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
                    "reason": tier_health.reason(f.health.kind),
                    # read again here: a restriction made since the snapshot holds now,
                    # and a snapshot kept through failed refreshes cannot hold it open
                    "restricted": f.restricted or is_restricted_cached(f.org, f.namespace),
                    # only a reader who may see the entry reads repo, branch and error
                    "repo": f.repo,
                    "branch": f.branch,
                    "error": tier_health.error_line(f.health.kind, f.health.error),
                    # the kind's fix without the repository it names: the problem is public
                    "fix": tier_health.fix_for(f.health.kind, f.health.error, name_where=False),
                    "severity": level,
                }
            )
    return tuple(sorted(rows, key=lambda r: (r["severity"] != "error", r["org"], r["namespace"])))
