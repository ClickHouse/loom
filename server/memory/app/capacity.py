"""Operator capacity readings with explicit units, freshness and scope."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from server.core import cell, pg
from server.core.ch import query_tag
from server.core.ch_errors import error_code
from server.core.ratelimit import admit_route
from server.core.repo_base import _CHClient
from server.identity.domain.read_scope import ReadFilters
from server.memory.store import capacity as store
from server.settings import settings

_reading = False


def model() -> dict[str, object]:
    """Historical decimal-byte baselines, never a current disk measurement."""
    return {
        "source": "historical small-data storage observation",
        "unit": "bytes_per_key",
        "clickhouse": 13_600,
        "postgres": 6_800,
        "scope_observation_threshold_rows": settings.capacity_scope_max_rows,
        "caveat": (
            "Approximate baseline for its measured vector dimensions and revision shape. "
            "History, other vector tables, objects, replicas, indexes, pending work and "
            "different dimensions change the footprint; N keys times this baseline is "
            "an estimate, not measured disk usage or a writes-per-second ceiling."
        ),
    }


def selection_valid(org: str | None, namespace: str | None) -> bool:
    """A paired explicit selector, never a wildcard or enumeration request."""
    if org is None and namespace is None:
        return True
    return cell.named_org(org) is not None and all(
        isinstance(value, str)
        and value
        and value not in cell.ORG_FREE
        and value == value.strip()
        and "*" not in value
        and not pg.unstorable(value)
        and not any(ord(char) < 32 for char in value)
        and len(value.encode("utf-8")) <= 255
        for value in (org, namespace)
    )


def _unknown(source: str, reason: str = "unavailable") -> dict[str, object]:
    return {"status": "unknown", "value": None, "source": source, "reason": reason}


def _date(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("missing observation time")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _rates(ch: _CHClient | None) -> dict[str, object]:
    source = "loom_telemetry.sli_minute"
    try:
        async with asyncio.timeout(settings.capacity_observation_timeout_s):
            row = await store.write_requests(ch=ch)
        if row is None or row["ingested_hwm"] is None:
            return _unknown(source, "rollup unavailable or disabled")
        mark, observed = _date(row["ingested_hwm"]), _date(row["observed_at"])
        start, end = _date(row["window_start"]), _date(row["window_end"])
        lag = (observed - mark).total_seconds()
        if not 0 <= lag <= settings.capacity_rollup_max_lag_s:
            return {
                **_unknown(source, "stale rollup"),
                "ingested_hwm": mark.isoformat(),
                "observed_at": observed.isoformat(),
                "lag_s": lag,
            }
        if end > mark or (end - start).total_seconds() != 300:
            raise ValueError("invalid rollup window")
        single, bulk = row["single_requests"], row["bulk_requests"]
        if any(type(n) is not int or n < 0 for n in (single, bulk)):
            raise ValueError("invalid request count")
        return {
            "status": "known",
            "source": source,
            "unit": "successful_requests_per_second",
            "single": single / 300,
            "bulk": bulk / 300,
            "single_requests": single,
            "bulk_requests": bulk,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "ingested_hwm": mark.isoformat(),
            "observed_at": observed.isoformat(),
            "lag_s": lag,
            "scope": "all serving processes writing this cell's SLI store",
            "committed_rows_per_second": None,
            "caveat": (
                "Request telemetry can lag or lose events; a bulk request can write many rows."
            ),
        }
    except Exception:  # noqa: BLE001 — unavailable telemetry is unknown, never zero
        return _unknown(source)


async def _count(read: Any, source: str) -> dict[str, object]:
    started = time.monotonic()
    try:
        async with asyncio.timeout(settings.capacity_observation_timeout_s):
            row = await read
        if (
            row is None
            or type(row["n"]) is not int
            or not 0 <= row["n"] <= settings.capacity_scope_max_rows + 1
        ):
            raise ValueError("invalid count")
        value = row["n"]
        return {
            "status": "at_least" if value > settings.capacity_scope_max_rows else "exact",
            "value": value,
            "source": source,
            "observed_at": _date(row["observed_at"]).isoformat(),
            "read_s": round(time.monotonic() - started, 6),
            "at_or_above_scan_policy": value >= settings.capacity_scope_max_rows,
        }
    except Exception as e:  # noqa: BLE001 — a bounded diagnostic never fabricates a count
        reason = (
            "scan_limit_hit"
            if isinstance(e, TimeoutError) or error_code(e) in (158, 159, 241, 307, 396)
            else "unavailable"
        )
        return {
            **_unknown(source, reason),
            "read_s": round(time.monotonic() - started, 6),
            "at_or_above_scan_policy": None,
        }


async def _scope_counts(
    org: str,
    namespace: str,
    scope: ReadFilters,
    ch: _CHClient | None,
) -> dict[str, object]:
    """Separate bounded observations; neither count substitutes for the other."""
    return {
        "org": org,
        "namespace": namespace,
        "read_scope": scope.scope,
        "live_heads": await _count(
            store.live_heads(org, namespace, scope=scope, ch=ch),
            "postgres.memory_current" if pg.enabled() else "clickhouse.memory FINAL",
        ),
        "vector_physical_rows": await _count(
            store.vector_rows(org, namespace, scope=scope, ch=ch), "clickhouse.memory_embeddings"
        ),
        "caveat": (
            "Separate point-in-time reads, not a commit admission count. Live heads use "
            "inventory's served status and tombstone predicates; expiry waits for its sweep. "
            "Vector rows include physical versions and tombstones in memory_embeddings "
            "only, not every vector table. FINAL may scan more versions than its output. "
            "Native limits can overshoot by blocks; a limited read is unknown."
        ),
    }


async def observe(
    org: str | None = None,
    namespace: str | None = None,
    *,
    scope: ReadFilters | None = None,
    scope_refused: bool = False,
    ch: _CHClient | None = None,
    authorize: Callable[[], Awaitable[ReadFilters]] | None = None,
) -> dict[str, object]:
    """Trusted adapters supply the authorization callback before any scope count."""
    global _reading
    out: dict[str, object] = {"model": model(), "scope": {"status": "not_requested"}}
    if _reading:
        out["write_requests"] = _unknown("loom_telemetry.sli_minute", "observation in progress")
        if org is not None or namespace is not None:
            out["scope"] = _unknown("selected namespace", "observation in progress")
        return out
    _reading = True
    try:
        # One fixed diagnostic bucket, rather than one entry per supplied selector.
        admit_route("insights.query", "-", org="*", actor=None, label="capacity")
        with query_tag("capacity.observe"):
            out["write_requests"] = await _rates(ch)
            if scope_refused or (bool(org) != bool(namespace)):
                out["scope"] = _unknown("selected namespace", "scope unavailable")
            elif org and namespace:
                if authorize is not None:
                    try:
                        async with asyncio.timeout(settings.capacity_observation_timeout_s):
                            scope = await authorize()
                    except Exception:  # noqa: BLE001 — no scope count follows an authorization failure
                        out["scope"] = _unknown("selected namespace", "scope unavailable")
                        return out
                if scope is None:
                    out["scope"] = _unknown("selected namespace", "scope unavailable")
                    return out
                out["scope"] = await _scope_counts(org, namespace, scope, ch)
        return out
    except Exception:  # noqa: BLE001 — pacing refusal is diagnostic unavailability
        out["write_requests"] = _unknown("loom_telemetry.sli_minute", "observation refused")
        if org is not None or namespace is not None:
            out["scope"] = _unknown("selected namespace", "observation refused")
        return out
    finally:
        _reading = False
