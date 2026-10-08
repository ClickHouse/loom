"""The folded reinforcement signal lives beside the vector table, not on it.

`loom.memory_reinforcement` holds the usage fold's absolute targets per
(org, namespace, agent, user_id, memory_key). The fold inserts a versioned row
(ReplacingMergeTree) instead of lightweight-UPDATEing `loom.memory_embeddings`,
so the vector table carries no patch parts and its codes-first read keeps its
lazy step. The fact leg reads this table after its fetch for
the candidates it returned and recomputes the decayed boost here, in Python,
with the same formula the SQL rank expression uses for the write-time base.

Everything that touches the table is in this module: the column set (the
RMT rule — every column on every row), the insert funnel, the keyed lookup
statement, and the boost arithmetic.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.datetimes import to_naive_utc as _naive
from server.memory.store.rows import next_version

log = logging.getLogger(__name__)

TABLE = "loom.memory_reinforcement"

# Canonical column list — mirrors the table's DDL. A row inserted
# without one of these lands the schema DEFAULT, which FINAL then surfaces
# over the prior real value; the builder below fills every column.
REINFORCEMENT_PERSISTED_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "agent": "-",
    "user_id": "-",
    "memory_key": "",
    "reinforcement_count": 1,
    "last_reinforced_at": None,
    "version": 0,
}


def build_reinforcement_row(**overrides: Any) -> dict[str, Any]:
    """A complete `loom.memory_reinforcement` row. Unknown column names raise:
    a typo'd override is a bug, not a silently dropped value."""
    unknown = [k for k in overrides if k not in REINFORCEMENT_PERSISTED_COLS]
    if unknown:
        raise ValueError(f"build_reinforcement_row: unknown columns {unknown}")
    row: dict[str, Any] = dict(REINFORCEMENT_PERSISTED_COLS)
    row.update(overrides)
    if row.get("last_reinforced_at") is None:
        row["last_reinforced_at"] = naive_utc_now()
    if not row.get("version"):
        row["version"] = next_version()
    return row


async def insert_reinforcement_rows(rows: list[dict[str, Any]], *, ch: Any) -> None:
    """The one funnel for the table's inserts: committed (not async) so a fold
    that returns has landed, the same setting the vector table's inserts use."""
    if not rows:
        return
    await ch.insert(TABLE, rows, settings={"async_insert": 0, "wait_for_async_insert": 1})


def reinforcement_lookup_sql() -> str:
    """The keyed post-fetch read: the newest folded values for the candidates'
    keys in one (org, namespace). A primary-key read (org, namespace lead the
    sorting key, memory_key is in it); argMax by version is the RMT collapse
    without FINAL, so the statement is one GROUP BY over a few dozen rows."""
    return f"""
        SELECT agent, user_id, memory_key,
               argMax(reinforcement_count, version) AS reinforcement_count,
               argMax(last_reinforced_at, version) AS last_reinforced_at
        FROM {TABLE}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key IN {{keys:Array(String)}}
        GROUP BY agent, user_id, memory_key
    """


async def fetch_reinforcement(
    ch: Any, *, org: str, namespace: str, memory_keys: list[str]
) -> dict[tuple[str, str, str], tuple[int, datetime]]:
    """`ch` is any client with `query_all(sql, params)` (the repo protocols differ
    only in parameter spelling, so this is structural by hand)."""
    # {(agent, user_id, memory_key): (count, last_reinforced_at)} for the keys.
    keys = sorted({str(k) for k in memory_keys if k})
    if not keys:
        return {}
    rows = await ch.query_all(
        reinforcement_lookup_sql(), params={"org": org, "ns": namespace, "keys": keys}
    )
    out: dict[tuple[str, str, str], tuple[int, datetime]] = {}
    for r in rows:
        last = r.get("last_reinforced_at")
        if not isinstance(last, datetime):
            continue
        ident = (
            str(r.get("agent") or ""),
            str(r.get("user_id") or ""),
            str(r.get("memory_key") or ""),
        )
        out[ident] = (int(r.get("reinforcement_count") or 1), last)
    return out


def _raw_reinforcement(
    count: int,
    last_reinforced_at: datetime | None,
    created_at: datetime | None,
    *,
    half_life_days: float,
    use_scale: float = 0.0,
    now: datetime | None = None,
) -> float:
    """ln(1+n) · decay with no weight and no cap — the quantity the fold
    scales by the pool's maximum."""
    n = max(float(count) - 1.0, 0.0)
    anchor = last_reinforced_at or created_at
    if anchor is None or n <= 0.0:
        return 0.0
    ref = _naive(now) if now is not None else naive_utc_now()
    days = float((ref.date() - _naive(anchor).date()).days)
    half_life = float(half_life_days) * (1.0 + float(use_scale) * math.log1p(n))
    return math.log1p(n) * math.exp(-days / max(half_life, 1.0))


# Row planes the fold knows. A row without `plane` is a fact
# row from the dense leg; a chunk pseudo-row carries `plane="chunk"` and the
# `parent_memory_key` its reinforcement is joined through.
PLANE_FACT = "fact"
PLANE_CHUNK = "chunk"

_Plan = tuple[dict[str, Any], int, datetime | None, datetime | None]


def _row_plane(r: dict[str, Any]) -> str:
    return str(r.get("plane") or PLANE_FACT)


def _folded_by_key(
    folded: dict[tuple[str, str, str], tuple[int, datetime]],
) -> dict[str, tuple[int, datetime]]:
    """The fold keyed by memory_key alone — the newest, largest count over
    the (agent, user_id) rows of a key. A chunk row joins its parent's
    reinforcement through this: the chunk table has
    no user_id column, so the triple lookup cannot apply."""
    out: dict[str, tuple[int, datetime]] = {}
    for (_agent, _user, key), (count, last) in folded.items():
        cur = out.get(key)
        if cur is None or count > cur[0] or (count == cur[0] and _naive(last) > _naive(cur[1])):
            out[key] = (count, last)
    return out


def _plan_rows(
    rows: list[dict[str, Any]],
    folded: dict[tuple[str, str, str], tuple[int, datetime]],
) -> list[_Plan]:
    """(row, count, last_reinforced, created) for every row: a row with no
    folded entry keeps its write-time base (the pool's zero when it has
    none)."""
    by_key = _folded_by_key(folded) if any(_row_plane(r) == PLANE_CHUNK for r in rows) else {}
    plan: list[_Plan] = []
    for r in rows:
        if _row_plane(r) == PLANE_CHUNK:
            hit = by_key.get(str(r.get("parent_memory_key") or ""))
        else:
            hit = folded.get(
                (
                    str(r.get("agent") or ""),
                    str(r.get("user_id") or ""),
                    str(r.get("memory_key") or ""),
                )
            )
        base_count = int(r.get("reinforcement_base") or 1)
        base_last = r.get("last_reinforced_base")
        last: datetime | None = base_last if isinstance(base_last, datetime) else None
        count = base_count
        if hit is not None:
            count = max(hit[0], base_count)
            last = hit[1]
            if isinstance(base_last, datetime) and _naive(base_last) > _naive(last):
                last = base_last
        plan.append((r, count, last, _parse_created(r.get("created_at"))))
    return plan


def _pool_boosts(raws: list[float], *, weight: float) -> list[float]:
    """weight · raw / max over the pool, centred on the mean share, so the
    pool's net lift is zero and only the order among its rows moves; a
    uniform or one-row pool gets nothing."""
    top = max(raws, default=0.0)
    shares = [(raw / top) if top > 0.0 else 0.0 for raw in raws]
    if not shares:
        return []
    mean_share = sum(shares) / len(shares)
    return [float(weight) * (share - mean_share) for share in shares]


def apply_folded_reinforcement(
    rows: list[dict[str, Any]],
    folded: dict[tuple[str, str, str], tuple[int, datetime]],
    *,
    weight: float,
    half_life_days: float,
    use_scale: float = 0.0,
    now: datetime | None = None,
) -> int:
    """Re-score `rows` with the folded counts, pool-centred: each row's share
    is (ln(1+n)·decay) / max over the candidate rows, centred on the pool's
    mean share and scaled by `weight`. The term ranks within the pool
    whatever the counts' absolute scale (the SQL term saturates at its cap
    past ~29 recalls), never exceeds `weight`, and lifts the pool by zero
    net. The SQL's own boost is replaced. Rows carrying `plane="chunk"` join
    through `parent_memory_key`. Returns the number of rows re-scored."""
    if weight <= 0.0:
        return 0
    plan = _plan_rows(rows, folded)
    raws = [
        _raw_reinforcement(
            c, last, created, half_life_days=half_life_days, use_scale=use_scale, now=now
        )
        for _r, c, last, created in plan
    ]
    boosts = _pool_boosts(raws, weight=weight)
    changed = 0
    for (r, _c, _l, _cr), new_boost in zip(plan, boosts, strict=True):
        old_boost = float(r.get("reinforcement_sql") or 0.0)
        if abs(new_boost - old_boost) < 1e-12:
            continue
        r["rank_score"] = float(r.get("rank_score") or 0.0) - old_boost + new_boost
        r["reinforcement_sql"] = new_boost
        changed += 1
    return changed


def _parse_created(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


__all__ = [
    "PLANE_CHUNK",
    "PLANE_FACT",
    "REINFORCEMENT_PERSISTED_COLS",
    "TABLE",
    "apply_folded_reinforcement",
    "build_reinforcement_row",
    "fetch_reinforcement",
    "insert_reinforcement_rows",
    "reinforcement_lookup_sql",
]
