"""Whether every replica knows `vector_search_use_quantized_codes`.

The setting name only exists on ClickHouse 26.7+, and an unknown setting
fails the whole statement, so no read carries it until a probe of every
replica says it is safe. `CHClient` turns the reading off when a replica
rejects the name mid-rollout.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Whether the connected ClickHouse knows `vector_search_use_quantized_codes`
# (26.7+). Filled by `probe_vector_search_settings()` at boot; None = not
# probed, treated as unsupported so a pre-26.7 server never sees the name.
_quantized_codes_supported: bool | None = None


def quantized_codes_supported() -> bool:
    return bool(_quantized_codes_supported)


def quantized_codes_state() -> bool | None:
    """The raw probe reading: None until a probe (or a test) sets it."""
    return _quantized_codes_supported


def set_quantized_codes_supported(value: bool | None) -> None:
    """Test seam and probe sink."""
    global _quantized_codes_supported
    _quantized_codes_supported = value


async def probe_vector_search_settings(ch: Any) -> bool:
    """Ask EVERY replica whether the two-stage codec setting exists.

    ClickHouse Cloud upgrades a service replica by replica, so `system.settings`
    on one node can list `vector_search_use_quantized_codes` while another node
    still rejects it with code 115 (say two replicas on 26.7.1, one on
    26.6.1). A single-replica answer therefore says nothing about the
    query that lands elsewhere; the setting is "supported" only when every
    replica reachable through `clusterAllReplicas('default', ...)` lists it.
    Where that table function is unavailable (a single OSS node without a
    `default` cluster) the local `system.settings` answers for the one server
    there is. Read-only and cheap; any failure stays "unsupported", which only
    costs the codec path on a server that may not have one.
    """
    supported = False
    try:
        rows = await ch.query_all(
            "SELECT count() AS replicas, "
            "countIf(has_setting) AS with_setting "
            "FROM (SELECT hostName() AS host, "
            "max(name = {n:String}) AS has_setting "
            "FROM clusterAllReplicas('default', system.settings) GROUP BY host)",
            {"n": "vector_search_use_quantized_codes"},
        )
        replicas = int(rows[0]["replicas"]) if rows else 0
        with_setting = int(rows[0]["with_setting"]) if rows else 0
        supported = replicas > 0 and with_setting == replicas
        if replicas and not supported:
            log.warning(
                "vector search settings probe: %d of %d replicas know "
                "vector_search_use_quantized_codes; mixed cluster, setting stays off",
                with_setting,
                replicas,
            )
    except Exception as e:  # noqa: BLE001 — fall back to the one server we can see
        log.info("clusterAllReplicas probe unavailable (%s); asking the local server", str(e)[:120])
        try:
            rows = await ch.query_all(
                "SELECT name FROM system.settings WHERE name = {n:String}",
                {"n": "vector_search_use_quantized_codes"},
            )
            supported = bool(rows)
        except Exception as e2:  # noqa: BLE001 — probe failed: quantized codes stay off
            log.warning("vector search settings probe failed (%s); quantized codes stay off", e2)
            supported = False
    set_quantized_codes_supported(supported)
    return supported


__all__ = [
    "probe_vector_search_settings",
    "quantized_codes_state",
    "quantized_codes_supported",
    "set_quantized_codes_supported",
]
