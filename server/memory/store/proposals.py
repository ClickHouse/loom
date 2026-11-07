"""The statements behind memory.proposals: the scan of proposed rows and what a listed
proposal shows. `where` and `params` are the listing's filters."""

from __future__ import annotations

from typing import Any

from server.memory.store.sql import _SEQUENTIAL, read_your_writes_enabled


def _sequential() -> str:
    # A re-read right after a decision must not see the decided row on a lagging replica.
    return _SEQUENTIAL if read_your_writes_enabled() else ""


async def scan_proposals(ch: Any, where: list[str], params: dict[str, Any]) -> list[Any]:
    """Newest first, at most `cap`: ids and keys only, so a short listing reads no values."""
    rows: list[Any] = await ch.query_all(
        f"""
        SELECT memory_key, toString(memory_id) AS mid
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC
        LIMIT {{cap:UInt32}}
        {_sequential()}
        """,
        params=params,
    )
    return rows


async def proposal_details(
    ch: Any, where: list[str], params: dict[str, Any], memory_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """What a listed proposal shows, for the scanned rows it returns, by memory id."""
    if not memory_ids:
        return {}
    rows = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS mid, agent, memory_type,
               -- a high value is readable by key only
               if(sensitivity = 'high', '', substring(value_preview, 1, 400)) AS preview,
               sensitivity, tags, toString(created_at) AS created, conflict_with
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)} AND memory_id IN {{ids:Array(UUID)}}
        {_sequential()}
        """,
        params={**params, "ids": memory_ids},
    )
    return {str(r["mid"]): r for r in rows}
