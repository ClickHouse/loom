"""Bounded reads of normalized query groups through a vetted read-only credential."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from server.core.ch_errors import is_transient_network_error
from server.core.errors import ValidationError
from server.core.hosts import vet_host
from server.core.remote_ch import connect
from server.core.secrets_at_rest import unseal
from server.settings import settings
from server.tracker.domain.source_circuit import SourceCircuit

PASSWORD_PURPOSE = "query_history_sources.password"
_BREAKERS: OrderedDict[str, SourceCircuit] = OrderedDict()


@asynccontextmanager
async def connection(source: dict[str, Any]) -> AsyncIterator[Any]:
    why, address = await vet_host(
        source["host"], allow_private=settings.tracker_self_service_allow_private_hosts
    )
    if why:
        raise ValidationError(why)
    enc = str(source["password_enc"])
    password = unseal(PASSWORD_PURPOSE, enc)
    if enc and not password:
        raise ValidationError("the query-history credential cannot be opened; register it again")
    timeout = settings.knowledge_query_history_read_timeout_s
    client = await asyncio.wait_for(
        connect(
            source["host"],
            address=address,
            port=source["port"],
            secure=source["secure"],
            database="system",
            user=source["user"],
            password=password,
            client_name="loom-query-history",
            send_receive_timeout=int(timeout) + 5,
            query_settings={
                "max_execution_time": timeout,
                "max_result_rows": 2000,
                "max_rows_to_read": 1_000_000,
                "max_memory_usage": 256_000_000,
                "max_result_bytes": 8_000_000,
                "result_overflow_mode": "throw",
            },
        ),
        timeout=timeout,
    )
    try:
        yield client
    finally:
        await client.close()


async def query(
    client: Any, sql: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result = await asyncio.wait_for(
        client.query(sql, parameters=params or {}),
        timeout=settings.knowledge_query_history_read_timeout_s,
    )
    return [dict(zip(result.column_names, row, strict=True)) for row in result.result_rows]


async def read_groups(source: dict[str, Any]) -> list[dict[str, Any]]:
    key = str(source["source_id"])
    breaker = _BREAKERS.setdefault(key, SourceCircuit(name=f"query-history/{key}"))
    _BREAKERS.move_to_end(key)
    while len(_BREAKERS) > settings.knowledge_query_history_max_breakers:
        _BREAKERS.popitem(last=False)
    if not breaker.allow():
        return []
    try:
        for attempt in range(2):
            try:
                async with connection(source) as client:
                    result = await query(
                        client,
                        group_sql(bool(source["cluster"])),
                        {
                            "cluster": source["cluster"],
                            "databases": source["databases"],
                            "days": settings.knowledge_query_history_window_days,
                            "k": settings.knowledge_query_history_min_users,
                            "lim": settings.knowledge_query_history_max_groups,
                        },
                    )
                break
            except Exception as e:
                if attempt or not is_transient_network_error(e):
                    raise
        breaker.record(True)
        return result
    except Exception:
        breaker.record(False)
        raise
    finally:
        breaker.release()


def group_sql(cluster: bool) -> str:
    table = (
        "clusterAllReplicas({cluster:String}, system.query_log)" if cluster else "system.query_log"
    )
    return f"""
        SELECT normalized_query_hash AS query_hash,
               normalizedQueryHashKeepNames(query) AS template_hash,
               normalizeQueryKeepNames(argMax(query, event_time_microseconds)) AS sql,
               uniqExact(initial_user) AS users, max(event_time) AS last_at
        FROM {table}
        WHERE type = 'QueryFinish' AND query_kind = 'Select' AND is_initial_query = 1
          AND event_date >= today() - toIntervalDay({{days:UInt32}})
          AND event_time > now() - toIntervalDay({{days:UInt32}})
          AND initial_user != '' AND notEmpty(databases)
          AND arrayAll(d -> has({{databases:Array(String)}}, d), databases)
        GROUP BY normalized_query_hash, template_hash
        HAVING users >= {{k:UInt32}} AND lengthUTF8(sql) <= 8000
        ORDER BY users DESC, last_at DESC, query_hash, template_hash
        LIMIT {{lim:UInt32}}
    """
