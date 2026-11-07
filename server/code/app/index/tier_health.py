"""Writes an entry's tier-health record (`server.code.domain.index.tier_health`).

A failure appends to the run of failures; a success after a failure clears it. A
success over a record that is already ok writes nothing, so a healthy entry costs one
state read per tier per pass. Recording never fails the pass it describes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from server.code.domain.index import tier_health
from server.core.datetimes import utc_now

if TYPE_CHECKING:
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
