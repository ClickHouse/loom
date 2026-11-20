"""Durable deduplication and narrowly scoped removal of budget warning events."""

from __future__ import annotations

import uuid
from typing import Any

from server.core.repo_base import _CHClient, _default_client

EVENT = "budget.warning"


async def event_seqs(
    org: str, inbox: str, event_id: str, *, ch: _CHClient | None = None
) -> set[int]:
    ch = ch or _default_client()
    rows = await ch.query_all(
        "SELECT seq FROM loom.task_events "
        "WHERE org = {org:String} AND inbox = {inbox:String} AND task_id = {id:UUID} "
        "AND event = {event:String} LIMIT 2 SETTINGS select_sequential_consistency = 1",
        params={"org": org, "inbox": inbox, "id": event_id, "event": EVENT},
    )
    return {int(row["seq"]) for row in rows}


async def insert_event(row: dict[str, Any], *, ch: _CHClient | None = None) -> bool:
    """Read back a deduplicated insert; retry with a fresh token if no row landed."""
    ch = ch or _default_client()
    event_id = str(row["task_id"])
    settings = {
        "async_insert": 0,
        "wait_for_async_insert": 1,
        "insert_deduplication_token": event_id,
    }
    await ch.insert("loom.task_events", [row], settings=settings)
    seqs = await event_seqs(row["org"], row["inbox"], event_id, ch=ch)
    if not seqs:
        settings["insert_deduplication_token"] = f"{event_id}:{uuid.uuid4().hex}"
        await ch.insert("loom.task_events", [row], settings=settings)
        seqs = await event_seqs(row["org"], row["inbox"], event_id, ch=ch)
    return int(row["seq"]) in seqs


async def remove_event(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    """An erasure reaching the observation invalidates only this delivery's row."""
    ch = ch or _default_client()
    await ch.execute(
        "DELETE FROM loom.task_events WHERE org = {org:String} AND inbox = {inbox:String} "
        "AND task_id = {id:UUID} AND seq = {seq:UInt64} AND event = {event:String}",
        params={
            "org": row["org"],
            "inbox": row["inbox"],
            "id": str(row["task_id"]),
            "seq": row["seq"],
            "event": EVENT,
        },
        settings={"lightweight_deletes_sync": 2},
    )
