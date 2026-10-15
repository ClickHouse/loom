"""The ClickHouse version floor for vector reads with the query condition cache on."""

from __future__ import annotations

from server.core.schema.preflight import version_key


def condition_cache_unsafe(oldest_version: str) -> bool:
    """Before 26.8 (ClickHouse #112086) a vector-search read with a WHERE clause
    could record granules as non-matching, and a later SELECT with the same
    condition silently returned fewer rows. True when the oldest replica is
    older than that, so Loom's sessions turn the cache off whatever any
    profile says."""
    return version_key(oldest_version)[:2] < (26, 8)
