"""The ClickHouse version floor for vector reads with the query condition cache on."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def warn_below_vector_floor(ch: Any) -> str | None:
    """ClickHouse 26.8 is the floor for a loom that runs vector reads with the
    query condition cache on: before #112086 a vector-search
    read with a WHERE clause could record granules as non-matching and a
    later ordinary SELECT with the same condition silently returned fewer
    rows. Logs a warning and returns it when the server is older and the
    cache is on; None otherwise. Read-only, never raises."""
    try:
        rows = await ch.query_all(
            "SELECT version() AS v, "
            "(SELECT value FROM system.settings WHERE name = 'use_query_condition_cache') AS qcc"
        )
        if not rows:
            return None
        version = str(rows[0]["v"])
        qcc = str(rows[0]["qcc"] or "0")
        parts = version.split(".")
        major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        if (major, minor) < (26, 8) and qcc in ("1", "true"):
            msg = (
                f"ClickHouse {version} with use_query_condition_cache = 1: vector-search "
                "reads can poison the condition cache before 26.8 (ClickHouse #112086); "
                "upgrade, or set use_query_condition_cache = 0 on loom's profile"
            )
            log.warning("vector floor: %s", msg)
            return msg
        return None
    except Exception as e:  # noqa: BLE001 — the floor warning is advisory
        log.info("vector floor probe skipped (%s)", str(e)[:120])
        return None
