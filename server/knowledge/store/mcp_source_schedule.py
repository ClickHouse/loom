"""Bounded MCP scheduling headers; source bodies stay in their existing store."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from server.core.ch import get_client, set_query_tag

# Random source_id prevents this worker-global digest from encoding a tenant address.
_KEY = "hex(SHA256(toJSONString(tuple(org, namespace, name, source_id))))"
_ELIGIBLE = """
    active AND source_kind = 'mcp' AND credential_mode = 'connected'
    AND source_contract_version = 1
    AND JSONExtractUInt(config_json, 'version') = 1
    AND JSONHas(config_json, 'paused') AND JSONType(config_json, 'paused') = 'Bool'
    AND NOT JSONExtractBool(config_json, 'paused')
    AND JSONExtractString(config_json, 'mode') IN ('snapshot', 'selected_refresh')
    AND source_id != ''
"""
PAGE_SQL = f"""
SELECT org, namespace, name, source_id,
       JSONExtractString(config_json, 'profile_id') AS profile_id,
       JSONExtractString(config_json, 'profile_sha256') AS profile_sha256,
       {_KEY} AS schedule_key
FROM loom.knowledge_sources FINAL
WHERE {_ELIGIBLE}
    AND schedule_key > {{after:String}}
    AND ({{org:String}} = '' OR org = {{org:String}})
    AND ({{ns:String}} = '' OR namespace = {{ns:String}})
    AND ({{only:String}} = '' OR name = {{only:String}})
ORDER BY schedule_key
LIMIT {{limit:UInt64}}
"""


class HeaderClient(Protocol):
    async def query_all(
        self, sql: str, params: dict[str, object], *, settings: dict[str, object]
    ) -> list[dict[str, object]]: ...


@dataclass(frozen=True)
class QueryLimits:
    rows: int
    bytes: int
    seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.rows) is not int
            or self.rows <= 0
            or type(self.bytes) is not int
            or self.bytes <= 0
            or not 0 < self.seconds < float("inf")
        ):
            raise ValueError("mcp_schedule_query_bound")


@dataclass(frozen=True, repr=False)
class Header:
    org: str
    namespace: str
    name: str
    source_id: str
    profile_id: str
    profile_sha256: str
    schedule_key: str


async def candidate_page(
    after: str,
    limit: int,
    *,
    query_limits: QueryLimits,
    org: str = "",
    namespace: str = "",
    only: str = "",
    ch: HeaderClient | None = None,
) -> list[Header]:
    """Metadata eligibility is a hint; the leased engine revalidates authority."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("mcp_schedule_page_bound")
    if after and re.fullmatch("[0-9A-F]{64}", after) is None:
        raise ValueError("mcp_schedule_cursor")
    if (namespace or only) and not org:
        raise ValueError("mcp_schedule_scope")
    if only and not namespace:
        raise ValueError("mcp_schedule_scope")
    set_query_tag("worker.knowledge_source_sync")
    rows = await (ch or get_client()).query_all(
        PAGE_SQL,
        params={"after": after, "limit": limit, "org": org, "ns": namespace, "only": only},
        settings={
            "max_rows_to_read": query_limits.rows,
            "max_bytes_to_read": query_limits.bytes,
            "max_execution_time": query_limits.seconds,
            "read_overflow_mode": "throw",
        },
    )
    if len(rows) > limit:
        raise ValueError("mcp_schedule_page_overflow")
    result = [Header(**{key: str(row[key]) for key in Header.__dataclass_fields__}) for row in rows]
    if any(
        re.fullmatch("[0-9A-F]{64}", row.schedule_key) is None
        or not row.source_id
        or row.schedule_key <= (result[i - 1].schedule_key if i else after)
        for i, row in enumerate(result)
    ):
        raise ValueError("mcp_schedule_page_order")
    return result
