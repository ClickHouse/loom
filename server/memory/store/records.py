"""Reads and writes of a key's active typed record: the fold's read, the supersede and
decision tombstone copies, and the record a decision revision builds on with its writer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

from server.core.datetimes import naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.decision_schema import SCHEMA_NAME
from server.memory.store.sql import _SEQUENTIAL

# The key's active record. Params: org, ns, schema, key.
_FROM_ACTIVE_RECORD = """
FROM loom.memory_records FINAL
WHERE org = {org:String}
  AND namespace = {ns:String}
  AND schema_name = {schema:String}
  AND memory_key = {key:String}
  AND status = 'active'
  AND tombstoned_at IS NULL
"""
# The key's live decision rows, under any identity. Params: org, ns, key.
_FROM_LIVE_DECISION_ROWS = """
FROM loom.memory FINAL
WHERE org = {org:String}
  AND namespace = {ns:String}
  AND memory_key = {key:String}
  AND memory_type = 'decision'
  AND tombstoned_at IS NULL
"""

# Computed columns take new names: `AS status` would shadow the column in the WHERE.
_CLOSE_ACTIVE_RECORD = f"""
INSERT INTO loom.memory_records
    (record_id, org, namespace, schema_id, schema_name, memory_key,
     memory_id, identity_agent, user_id, session_id, record_json,
     status, valid_at, valid_to, version, created_at, updated_at,
     tombstoned_at)
SELECT
    record_id, org, namespace, schema_id, schema_name, memory_key,
    memory_id, identity_agent, user_id, session_id, record_json,
    {{status:String}} AS new_status, valid_at,
    if(valid_to IS NULL, {{close:DateTime}}, valid_to) AS new_valid_to,
    greatest(version + 1, {{ver:UInt64}}) AS new_version, created_at,
    greatest(updated_at, {{now:DateTime}}) AS new_updated_at,
    {{tombstoned_at:Nullable(DateTime)}} AS new_tombstoned_at
{_FROM_ACTIVE_RECORD}"""

# Retires the record unless its writer still has a live decision row under the key.
_TOMBSTONE_DECISION_RECORD = f"""{_CLOSE_ACTIVE_RECORD}
  AND (identity_agent, user_id, session_id) NOT IN (
      SELECT agent, user_id, session_id {_FROM_LIVE_DECISION_ROWS}
  )
{_SEQUENTIAL}"""

# The active record and its writer, when that writer still has a live decision row under
# the key.
_LIVE_WRITER_RECORD = f"""
SELECT identity_agent, user_id, session_id, record_json, created_at
{_FROM_ACTIVE_RECORD}
  AND (identity_agent, user_id, session_id) IN (
      SELECT agent, user_id, session_id {_FROM_LIVE_DECISION_ROWS}
  )
ORDER BY version DESC
LIMIT 1
{_SEQUENTIAL}"""

# The key's newest active record.
_ACTIVE_RECORD_ROW = f"""
SELECT toString(record_id) AS rid, toString(memory_id) AS mid, record_json
{_FROM_ACTIVE_RECORD}
ORDER BY version DESC
LIMIT 1"""


class WriterRecord(NamedTuple):
    """An active decision record with the identity that wrote it."""

    agent: str
    user_id: str
    session_id: str
    record_json: str
    created_at: datetime


async def supersede_active_record(
    org: str,
    namespace: str,
    schema_name: str,
    memory_key: str,
    *,
    close_at: datetime,
    version: int,
    now: datetime,
    ch: _CHClient | None = None,
) -> None:
    """Close the key's active record ahead of a new one written at `version`."""
    ch = ch or _default_client()
    await ch.execute(
        _CLOSE_ACTIVE_RECORD,
        params={
            "org": org,
            "ns": namespace,
            "schema": schema_name,
            "key": memory_key,
            "status": "superseded",
            "close": close_at,
            "ver": version,
            "now": now,
            "tombstoned_at": None,
        },
    )


async def tombstone_decision_record(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    version: int,
    ch: _CHClient | None = None,
) -> None:
    """Tombstone the key's active decision record unless its writer still has a live
    decision row there."""
    ch = ch or _default_client()
    now = naive_utc_now_seconds()
    await ch.execute(
        _TOMBSTONE_DECISION_RECORD,
        params={
            "org": org,
            "ns": namespace,
            "schema": SCHEMA_NAME,
            "key": memory_key,
            "status": "tombstoned",
            "close": now,
            "ver": version,
            "now": now,
            "tombstoned_at": now,
        },
    )


async def active_record(
    org: str,
    namespace: str,
    schema_name: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """The key's newest active record as {rid, mid, record_json}, or None."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        _ACTIVE_RECORD_ROW,
        {"org": org, "ns": namespace, "schema": schema_name, "key": memory_key},
    )
    return rows[0] if rows else None


async def live_writer_record(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> WriterRecord | None:
    """The active decision record under `memory_key` and its writer, read together, or
    None when there is no active record or its writer has no live decision row there."""
    ch = ch or _default_client()
    row = await ch.query_one(
        _LIVE_WRITER_RECORD,
        params={"org": org, "ns": namespace, "schema": SCHEMA_NAME, "key": memory_key},
    )
    if row is None:
        return None
    return WriterRecord(
        row["identity_agent"],
        row["user_id"],
        row["session_id"],
        row["record_json"],
        row["created_at"],
    )
