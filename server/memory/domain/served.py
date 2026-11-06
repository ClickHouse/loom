"""How often agents were shown a memory, from its usage events.

An agent read writes one event per returned key: a `recall` credit when its reader is
credited (the ranking signal folded into `recall_count`), else a `served` event. The figure
a person reads as "recalled" counts both. `recall_count` can also hold a count seeded by a
bulk import with no events behind it, so it is the floor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from server.core.datetimes import UNIX_EPOCH, to_naive_utc


def served_totals(
    agent_rows: Sequence[Mapping[str, Any]],
    recall_count: int,
    last_recalled_at: datetime | None,
) -> dict[str, Any]:
    """`served_count` and `last_served_at` from the per-agent rows of
    `usage.served_by_agent` (`n`, `last_at`) and the row's own credit columns.
    `last_served_at` is None when no read served the key."""
    times = [to_naive_utc(r["last_at"]) for r in agent_rows if r.get("last_at")]
    events = sum(int(r.get("n") or 0) for r in agent_rows)
    # A bulk import may have stored the epoch for "never": it is no time at all.
    if last_recalled_at and to_naive_utc(last_recalled_at) > to_naive_utc(UNIX_EPOCH):
        times.append(to_naive_utc(last_recalled_at))
    return {
        "served_count": max(int(recall_count or 0), events),
        "last_served_at": max(times) if times else None,
    }
