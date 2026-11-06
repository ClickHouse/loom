"""How often agents were shown a memory, from its usage events.

An agent read writes one event per returned key: a `recall` credit when its reader is
credited (the ranking signal folded into `recall_count`), else a `served` event. The figure
a person reads as "recalled" counts both. `recall_count` can also hold a count seeded by a
bulk import with no events behind it, so it is the floor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from server.core.datetimes import UNIX_EPOCH, to_naive_utc


def served_totals(
    agent_rows: Iterable[Mapping[str, Any]],
    recall_count: int,
    last_recalled_at: datetime | None,
) -> dict[str, Any]:
    """`served_count` and `last_served_at` from the per-agent rows of
    `usage.served_by_agent` (`n`, `last_at`) and the row's own credit columns.
    `last_served_at` is None when no read served the key."""
    count = int(recall_count or 0)
    events = 0
    times = [to_naive_utc(last_recalled_at)] if last_recalled_at else []
    for r in agent_rows:
        events += int(r.get("n") or 0)
        if r.get("last_at"):
            times.append(to_naive_utc(r["last_at"]))
    # A row with no event of the kind reads the epoch; it is no time at all.
    epoch = to_naive_utc(UNIX_EPOCH)
    seen = [t for t in times if t is not None and t > epoch]
    return {"served_count": max(count, events), "last_served_at": max(seen) if seen else None}
