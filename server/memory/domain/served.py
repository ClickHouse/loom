"""How often agents were shown a memory, from its served reads.

Every agent read that returns a key, credited or not, adds a served row
(`loom.memory_served_daily`); the figure a person reads as "recalled" is their count.
`recall_count` (the ranking credit) can hold a count a bulk import seeded, or credits
written before served rows were recorded, with no served rows behind them, so it is the
floor, and its time stands in when no served row is newer.
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
    `store.served.served_by_agent` (`n`, `last_at`) and the row's own credit columns.
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


def hit_row(hit: Any, namespace: str) -> tuple[str, str]:
    """The (namespace, memory_key) of the stored row a search hit came from: a chunk hit
    (`chunk:<session>:<n>`) is its parent fact's; a hit with no namespace is the read's."""
    key = getattr(hit, "parent_memory_key", "") or hit.memory_key
    return (str(getattr(hit, "namespace", "") or namespace), str(key))
