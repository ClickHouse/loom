"""Blacklist sweep: a pattern reaches the rows written before it.

`blacklist.add` refuses future writes (422 on `memory.set`, dropped entries on the bulk
path) and filters search. A row stored before the pattern existed stays in the table,
and readers that do not go through search still see it. The sweep finds the live rows a
pattern matches — inside the caller's org, and inside the namespace for a
namespace-scoped pattern — and tombstones them through the one by-id funnel that also
mirrors the tombstone onto the embeddings table, leaving a `memory_drops` row per
pattern so the ledger says what a Refuse removed, and a `memory.tombstoned` event per
row for the namespace's webhooks. A token narrowed to one user never
reaches a sweep (no blacklist op is in NARROW_SAFE_OPS).

Matching is the SQL twin of `server.memory.app.blacklist_guard._matches`: `position(value,
pattern) > 0` for a substring pattern (case-sensitive, like the in-process
check) and `match(value, pattern)` for a regex. Both run RE2, and the write path
compiles with `.` matching a newline as ClickHouse does, so a sweep tombstones the
rows a write of the same value would be refused for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from server.core.datetimes import naive_utc_now
from server.memory.app import webhooks
from server.memory.domain.expire_reasons import SWEEP_REASON_PREFIX
from server.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepResult:
    pattern_id: str
    matched: int
    tombstoned: int
    truncated: bool
    error: str = ""


def sweep_where(pattern_type: str, scope: str, namespace: str) -> tuple[str, dict[str, Any]]:
    """The predicate's variable part and its params (org/pattern added by caller)."""
    parts = ["org = {org:String}", "tombstoned_at IS NULL", "status != 'tombstoned'"]
    params: dict[str, Any] = {}
    if scope == "namespace":
        parts.append("namespace = {ns:String}")
        params["ns"] = namespace
    if pattern_type == "regex":
        parts.append("match(value, {pat:String})")
    else:
        parts.append("position(value, {pat:String}) > 0")
    return " AND ".join(parts), params


async def sweep_pattern(
    *,
    org: str,
    namespace: str,
    pattern_id: str,
    pattern: str,
    pattern_type: str,
    scope: str,
    limit: int | None = None,
    ch: Any = None,
) -> SweepResult:
    if limit is None:
        limit = settings.memory_blacklist_sweep_limit
    import server.memory.store as memory_repo
    from server.core.repo_base import _default_client
    from server.memory.app.drops import record_drop

    ch = ch or _default_client()
    where, params = sweep_where(pattern_type, scope, namespace)
    params.update({"org": org, "pat": pattern, "lim": int(limit) + 1})
    try:
        rows = await ch.query_all(
            f"""
            SELECT memory_id, namespace FROM loom.memory FINAL
            WHERE {where}
            LIMIT {{lim:UInt32}}
            """,
            params=params,
        )
    except Exception as e:  # noqa: BLE001 — a bad regex must not break the add
        log.warning("blacklist sweep %s: match query failed: %s", pattern_id, e)
        return SweepResult(pattern_id, 0, 0, False, error=str(e)[:200])
    truncated = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return SweepResult(pattern_id, 0, 0, False)
    when = naive_utc_now()
    ids = [str(r["memory_id"]) for r in rows]
    tombstoned = await memory_repo.tombstone_cascade_by_ids(
        org,
        ids,
        when=when,
        update=lambda _existing: {"expire_reason": f"{SWEEP_REASON_PREFIX}{pattern_id}"[:512]},
        ch=ch,
    )
    await webhooks.announce([("memory.tombstoned", change) for change in tombstoned])
    by_ns: dict[str, int] = {}
    for r in rows:
        by_ns[str(r["namespace"])] = by_ns.get(str(r["namespace"]), 0) + 1
    for ns, n in by_ns.items():
        record_drop(org, ns, "blacklist.sweep", f"pattern:{pattern_id}", n=n)
    return SweepResult(pattern_id, len(rows), len(tombstoned), truncated)
