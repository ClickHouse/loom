"""Full-row process registrations and explicit transition acknowledgements."""

from time import time_ns
from typing import Any
from uuid import UUID

from server.core.repo_base import _CHClient, _default_client


async def participants(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    return await (ch or _default_client()).query_all(
        "SELECT participant_id, transition_id, state, version "
        "FROM loom._provider_participants FINAL WHERE state != 'retired'"
    )


async def record_participant(
    identity: UUID, transition: UUID, state: str, *, after: int = 0, ch: _CHClient | None = None
) -> None:
    if state not in ("running", "drained", "retired"):
        raise ValueError("Unknown participant state")
    await (ch or _default_client()).insert(
        "loom._provider_participants",
        [
            {
                "participant_id": identity,
                "transition_id": transition,
                "state": state,
                "version": max(time_ns(), after + 1),
            }
        ],
    )


async def storage_idle(*, ch: _CHClient | None = None) -> bool:
    """Conservative pinned-node check, after producers positively stopped."""
    client = ch or _default_client()
    checks = (
        "SELECT count() AS running FROM system.processes WHERE query_id != currentQueryID() "
        "AND (current_database = 'loom' OR positionCaseInsensitive(query, 'loom.') > 0)",
        "SELECT count() AS running FROM system.mutations WHERE database = 'loom' AND NOT is_done",
        "SELECT count() AS running FROM system.asynchronous_inserts WHERE database = 'loom'",
    )
    for sql in checks:
        row = await client.query_one(sql)
        if row is None or type(row.get("running")) is not int or row["running"] != 0:
            return False
    return True
