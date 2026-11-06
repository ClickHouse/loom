"""memory.health — is the knowledge base earning its keep?

Reports coverage by type, top recalled entries, silent entries (no recall credit, and not
served to an agent in the last `silent_after_days`) and the staleness of live rows, so a
namespace where most rows are never read shows it. One query over loom.memory FINAL joined
to the summed loom.memory_usage_totals and to loom.memory_served_daily's last
`silent_after_days` days, scoped to (org, namespace). A key counts as read when any of its
live rows carries a recall credit or a served row in that window: a read lands on one copy
of the key only (the newest inside the reader's scope).
"""

from __future__ import annotations

from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.memory.api.models_admin import MemoryHealthInput, MemoryHealthOutput

_LIVE = """
    SELECT m.memory_id AS memory_id, m.memory_key AS memory_key,
           m.memory_type AS memory_type, m.created_at AS created_at,
           m.expires_at AS expires_at, m.valid_to AS valid_to,
           m.invalid_at AS invalid_at, m.pinned AS pinned,
           m.last_reinforced_at AS last_reinforced_at,
           m.value_chars AS value_chars,
           coalesce(u.recall_count, 0) AS recall_count,
           coalesce(u.used_count, 0) AS used_count,
           coalesce(u.harm_count, 0) AS harm_count,
           u.last_recalled_at AS last_recalled_at,
           coalesce(s.served_count, 0) AS served_count
    FROM (
        SELECT memory_id, memory_key, memory_type, created_at,
               expires_at, valid_to, invalid_at, pinned, last_reinforced_at,
               length(value) AS value_chars
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND tombstoned_at IS NULL AND status = 'active'
    ) AS m
    LEFT JOIN (
        SELECT memory_id, sum(recall_count) AS recall_count, sum(used_count) AS used_count,
               sum(harm_count) AS harm_count,
               max(last_recalled_at) AS last_recalled_at
        FROM loom.memory_usage_totals
        WHERE org = {org:String} AND namespace = {ns:String}
        GROUP BY memory_id
    ) AS u ON u.memory_id = m.memory_id
    LEFT JOIN (
        SELECT memory_id, sum(served) AS served_count
        FROM loom.memory_served_daily
        WHERE org = {org:String} AND namespace = {ns:String}
          AND day > today() - {days:UInt32}
        GROUP BY memory_id
    ) AS s ON s.memory_id = m.memory_id
"""


def footprint(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure: how much the live rows hold. `value_chars` is the summed length of every
    stored value; `value_tokens_est` is chars/4, an estimate (named so), never a tokenizer
    count. A bench reads the memory footprint back from here instead of paging
    memory.list, which is scoped to one identity tuple."""
    chars = sum(int(r.get("value_chars") or 0) for r in rows)
    return {
        "value_chars": chars,
        "value_tokens_est": chars // 4,
        "tokens_per_row_est": round(chars / 4 / len(rows), 1) if rows else 0.0,
    }


def summarise(rows: list[dict[str, Any]], *, silent_after_days: int, top_k: int) -> dict[str, Any]:
    """Pure: the health figures from joined (memory × usage) rows."""
    from datetime import datetime, timedelta

    cutoff = naive_utc_now() - timedelta(days=silent_after_days)
    live = len(rows)

    reads_by_key: dict[str, int] = {}
    for r in rows:
        key = str(r.get("memory_key") or "")
        reads = int(r.get("recall_count") or 0) + int(r.get("served_count") or 0)
        reads_by_key[key] = reads_by_key.get(key, 0) + reads

    def _read(r: dict[str, Any]) -> bool:
        return reads_by_key.get(str(r.get("memory_key") or ""), 0) > 0

    recalled = [r for r in rows if int(r.get("recall_count") or 0) > 0]
    used = sum(1 for r in rows if int(r.get("used_count") or 0) > 0)
    # Relied on in a run that ended badly. A row both used and harmed is a candidate for
    # review, not deletion.
    harmed = sum(1 for r in rows if int(r.get("harm_count") or 0) > 0)
    silent_rows = [r for r in rows if not _read(r)]
    silent_older = sum(
        1
        for r in silent_rows
        if isinstance(r.get("created_at"), datetime)
        and r["created_at"].replace(tzinfo=None) < cutoff
    )
    by_type: dict[str, dict[str, int]] = {}
    for r in rows:
        t = str(r.get("memory_type") or "")
        d = by_type.setdefault(t, {"live": 0, "recalled": 0})
        d["live"] += 1
        if _read(r):
            d["recalled"] += 1
    top = sorted(recalled, key=lambda r: (-int(r["recall_count"]), -int(r.get("used_count") or 0)))
    return {
        "staleness": staleness(rows),
        "footprint": footprint(rows),
        "live": live,
        "recalled_ever": sum(1 for r in rows if _read(r)),
        "used_ever": used,
        "harmed_ever": harmed,
        "silent": len(silent_rows),
        "silent_older": silent_older,
        "silent_share": round(len(silent_rows) / live, 4) if live else 0.0,
        "silent_after_days": silent_after_days,
        "by_type": [
            {"memory_type": t, **v}
            for t, v in sorted(by_type.items(), key=lambda kv: (-kv[1]["live"], kv[0]))
        ],
        "top_recalled": [
            {
                "memory_key": str(r.get("memory_key") or ""),
                "memory_type": str(r.get("memory_type") or ""),
                "recall_count": int(r["recall_count"]),
                "used_count": int(r.get("used_count") or 0),
                "harm_count": int(r.get("harm_count") or 0),
                "last_recalled_at": r.get("last_recalled_at"),
            }
            for r in top[:top_k]
        ],
    }


# Age buckets for the staleness distribution (days since created_at).
_AGE_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("lt7d", 0, 7),
    ("d7_30", 7, 30),
    ("d30_90", 30, 90),
    ("gt90d", 90, None),
)


def staleness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure: the staleness distribution of live rows: age buckets, and how many carry a
    validity end, an expiry or a pin. When almost no row has a validity end, an `as_of`
    read over active rows answers the same as latest; this makes that visible."""
    from datetime import datetime, timedelta

    now = naive_utc_now()
    live = len(rows)

    def _dt(v: Any) -> datetime | None:
        return v.replace(tzinfo=None) if isinstance(v, datetime) else None

    ages = [(now - c).days for r in rows if (c := _dt(r.get("created_at"))) is not None]
    by_age = {name: 0 for name, _lo, _hi in _AGE_BUCKETS}
    for a in ages:
        for name, lo, hi in _AGE_BUCKETS:
            if a >= lo and (hi is None or a < hi):
                by_age[name] += 1
                break
    with_validity_end = sum(
        1
        for r in rows
        if _dt(r.get("valid_to")) is not None or _dt(r.get("invalid_at")) is not None
    )
    with_expiry = sum(1 for r in rows if _dt(r.get("expires_at")) is not None)
    expiring_7d = sum(
        1
        for r in rows
        if (e := _dt(r.get("expires_at"))) is not None and e <= now + timedelta(days=7)
    )
    pinned = sum(1 for r in rows if bool(r.get("pinned")))
    # Days since the row was last reinforced (write-time counts as reinforced);
    # the reinforcement half-life in ranking decays from this moment.
    since_reinforced = [
        (now - lr).days
        for r in rows
        if (lr := _dt(r.get("last_reinforced_at"))) is not None and lr.year > 1971
    ]
    since_reinforced.sort()

    def _pct(xs: list[int], q: float) -> int:
        return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0

    def _share(n: int) -> float:
        return round(n / live, 4) if live else 0.0

    return {
        "by_age": by_age,
        "with_validity_end": with_validity_end,
        "with_validity_end_share": _share(with_validity_end),
        "with_expiry": with_expiry,
        "expiring_7d": expiring_7d,
        "pinned": pinned,
        "days_since_reinforced_p50": _pct(since_reinforced, 0.5),
        "days_since_reinforced_p90": _pct(since_reinforced, 0.9),
    }


async def handle_memory_health(input: MemoryHealthInput) -> MemoryHealthOutput:
    rows = await get_client().query_all(
        _LIVE, {"org": input.org, "ns": input.namespace, "days": input.silent_after_days}
    )
    return MemoryHealthOutput(
        **summarise(
            [dict(r) for r in rows],
            silent_after_days=input.silent_after_days,
            top_k=input.top_k,
        )
    )
