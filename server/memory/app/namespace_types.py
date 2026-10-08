"""Per-memory_type config lookup against loom.namespace_types.

Empty values everywhere mean the caller falls back to namespace-level defaults,
so a missing row and a missing service both degrade to the same path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TypeConfig:
    """One row from loom.namespace_types."""

    retention_days: int  # 0 ⇒ no per-type override
    dedup_threshold: float | None
    hot_quota: int
    conflict_policy: str  # "" ⇒ inherit namespace default


_EMPTY = TypeConfig(retention_days=0, dedup_threshold=None, hot_quota=0, conflict_policy="")


async def get_type_config(
    org: str,
    namespace: str,
    memory_type: str,
) -> TypeConfig:
    """Return the config row for (org, namespace, memory_type), or empty when none."""
    if not memory_type:
        return _EMPTY
    from server.core.ch import get_client

    try:
        row = await get_client().query_one(
            """
            SELECT
                retention_days,
                dedup_threshold,
                hot_quota,
                conflict_policy
            FROM loom.namespace_types FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND memory_type = {mt:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace, "mt": memory_type},
        )
    except Exception as e:  # noqa: BLE001 — a failed type read falls back to namespace defaults
        log.warning("namespace_types lookup failed (%s); falling back to namespace defaults", e)
        return _EMPTY
    if not row:
        return _EMPTY
    return TypeConfig(
        retention_days=int(row.get("retention_days") or 0),
        dedup_threshold=(
            float(row["dedup_threshold"]) if row.get("dedup_threshold") is not None else None
        ),
        hot_quota=int(row.get("hot_quota") or 0),
        conflict_policy=str(row.get("conflict_policy") or ""),
    )


async def list_type_configs(
    org: str,
    namespace: str,
) -> list[dict[str, Any]]:
    """Return all rows for one namespace. Used by `namespace.get`."""
    from server.core.ch import get_client

    try:
        rows = await get_client().query_all(
            """
            SELECT
                memory_type,
                retention_days,
                dedup_threshold,
                hot_quota,
                conflict_policy
            FROM loom.namespace_types FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            ORDER BY memory_type
            """,
            params={"org": org, "ns": namespace},
        )
    except Exception as e:  # noqa: BLE001 — a failed type list reads as no types
        log.warning("namespace_types list failed: %s", e)
        return []
    return [
        {
            "memory_type": r["memory_type"],
            "retention_days": int(r.get("retention_days") or 0),
            "dedup_threshold": (
                float(r["dedup_threshold"]) if r.get("dedup_threshold") is not None else None
            ),
            "hot_quota": int(r.get("hot_quota") or 0),
            "conflict_policy": str(r.get("conflict_policy") or ""),
        }
        for r in rows
    ]
