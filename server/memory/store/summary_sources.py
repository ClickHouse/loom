"""Source-state checks for the session-summary shortlist."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core import pg
from server.core.datetimes import to_naive_utc
from server.core.hashing import hash_text
from server.core.repo_base import _CHClient, query_all_sliced
from server.memory.domain.redaction_lineage import is_content_hash
from server.memory.domain.temporal_sql import (
    as_of_predicate,
    status_predicate,
    tombstone_predicate,
)
from server.memory.store import pg_memory

SourceKey = tuple[str, str, str, str]


def _sources(row: dict[str, Any]) -> list[SourceKey]:
    agent, user, session = (
        str(row.get(axis) or "-") for axis in ("agent", "user_id", "session_id")
    )
    return [(agent, user, session, str(key)) for key in row.get("derived_from", []) if key]


def _source_state(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        hash_text(value)
        if col == "content_sha256"
        else to_naive_utc(value)
        if isinstance(value, datetime)
        else value
        for col in pg_memory.SUMMARY_SOURCE_STATE_COLUMNS
        for value in [row.get(col)]
    )


async def current_summary_sources(
    ch: _CHClient,
    rows: list[dict[str, Any]],
    *,
    org: str,
    namespace: str,
    as_of: Any = None,
    as_of_basis: str = "valid_time",
) -> list[dict[str, Any]]:
    """Withhold digests of known unserved or rewritten sources, within the full identity.

    A digest has no source revision hashes, so a later version conservatively invalidates
    it. Missing lineage remains session evidence; it is not proof of a current fact.
    """
    sources = sorted({key for row in rows for key in _sources(row)})
    if not sources:
        return rows
    replaying = as_of is not None
    state = [
        tombstone_predicate(as_of_basis, replaying=replaying),
        status_predicate(as_of_basis, replaying=replaying),
        "sensitivity != 'high'",
        "redacted_at IS NULL",
        "(expires_at IS NULL OR expires_at > now())",
        "(valid_to IS NULL OR valid_to > now())",
    ]
    params: dict[str, Any] = {"org": org, "ns": namespace, "sources": sources}
    if replaying:
        params["as_of"] = as_of
        state.append(as_of_predicate(as_of_basis))
    parents = await query_all_sliced(
        ch,
        f"""
        SELECT agent, if(user_id = '', '-', user_id) AS source_user,
               if(session_id = '', '-', session_id) AS source_session, memory_key,
               {", ".join(pg_memory.SUMMARY_SOURCE_STATE_COLUMNS)},
               {" AND ".join(state)} AS source_served
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND (agent, if(user_id = '', '-', user_id), if(session_id = '', '-', session_id),
               memory_key) IN {{sources:Array(Tuple(String,String,String,String))}}
        """,
        params,
        "sources",
    )
    authority = (
        await pg_memory.summary_source_heads([(org, namespace, *key) for key in sources])
        if pg.enabled()
        else None
    )
    heads: dict[SourceKey, tuple[bool, int]] = {}
    for p in parents:
        key = (
            str(p["agent"]),
            str(p["source_user"]),
            str(p["source_session"]),
            str(p["memory_key"]),
        )
        served, version = heads.get(key, (True, 0))
        current = True
        if authority is not None:
            head = authority.get((org, namespace, *key))
            current = (
                head is not None
                and is_content_hash(str(head.get("content_sha256") or ""))
                and _source_state(head) == _source_state(p)
            )
        heads[key] = (
            served and bool(p["source_served"]) and current,
            max(version, int(p["version"])),
        )
    return [
        row
        for row in rows
        if all(
            key in heads and heads[key][0] and heads[key][1] <= int(row["version"])
            for key in _sources(row)
        )
    ]
