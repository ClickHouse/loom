"""Active typed records of loom.memory_records: reads, and the supersede and decision
tombstone copies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

from server.core.datetimes import naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.decision_schema import SCHEMA_NAME
from server.memory.store.sql import _SEQUENTIAL

# Active records of a schema; the reads below add the key condition.
_FROM_ACTIVE_OF_SCHEMA = """
FROM loom.memory_records FINAL
WHERE org = {org:String}
  AND namespace = {ns:String}
  AND schema_name = {schema:String}
  AND status = 'active'
  AND tombstoned_at IS NULL
"""
_FROM_ACTIVE_OF_KEY = _FROM_ACTIVE_OF_SCHEMA + "  AND memory_key = {key:String}\n"
# The key's live decision rows, under any identity: the tombstone check and the writer read.
_FROM_LIVE_DECISION_ROWS = """
FROM loom.memory FINAL
WHERE org = {org:String}
  AND namespace = {ns:String}
  AND memory_key = {key:String}
  AND memory_type = 'decision'
  AND tombstoned_at IS NULL
"""

# Computed columns take new names: `AS status` would shadow the column in the WHERE.
_CLOSE_ACTIVE_RECORD = (
    """
INSERT INTO loom.memory_records
    (record_id, org, namespace, schema_id, schema_name, memory_key,
     memory_id, identity_agent, user_id, session_id, record_json,
     status, valid_at, valid_to, version, created_at, updated_at,
     tombstoned_at)
SELECT
    record_id, org, namespace, schema_id, schema_name, memory_key,
    memory_id, identity_agent, user_id, session_id, record_json,
    {status:String} AS new_status, valid_at,
    if(valid_to IS NULL, {close:DateTime}, valid_to) AS new_valid_to,
    greatest(version + 1, {ver:UInt64}) AS new_version, created_at,
    greatest(updated_at, {now:DateTime}) AS new_updated_at,
    {tombstoned_at:Nullable(DateTime)} AS new_tombstoned_at"""
    + _FROM_ACTIVE_OF_KEY
)

# Retires the record unless its writer still has a live decision row under the key.
_TOMBSTONE_DECISION_RECORD = f"""{_CLOSE_ACTIVE_RECORD}
  AND (identity_agent, user_id, session_id) NOT IN (
      SELECT agent, user_id, session_id {_FROM_LIVE_DECISION_ROWS}
  )
{_SEQUENTIAL}"""

# The newest active record whose writer still has a live decision row under the key.
_LIVE_WRITER_RECORD = f"""
SELECT identity_agent, user_id, session_id, record_json, created_at
{_FROM_ACTIVE_OF_KEY}
  AND (identity_agent, user_id, session_id) IN (
      SELECT agent, user_id, session_id {_FROM_LIVE_DECISION_ROWS}
  )
ORDER BY version DESC
LIMIT 1
{_SEQUENTIAL}"""

# The key's newest active record.
_ACTIVE_RECORD_ROW = f"""
SELECT toString(record_id) AS rid, toString(memory_id) AS mid, record_json
{_FROM_ACTIVE_OF_KEY}
ORDER BY version DESC
LIMIT 1"""

# The active decision records of several keys.
_ACTIVE_DECISION_RECORDS = (
    "SELECT memory_key, record_json, created_at"
    + _FROM_ACTIVE_OF_SCHEMA
    + "  AND memory_key IN {keys:Array(String)}\n"
)


class ActiveRecord(NamedTuple):
    """The key's newest active record, as the record write's fold reads it."""

    record_id: str
    memory_id: str | None
    record_json: str


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
    decision row there. It writes no other row, so it reads the clock itself."""
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
) -> ActiveRecord | None:
    """The key's newest active record, or None."""
    ch = ch or _default_client()
    row = await ch.query_one(
        _ACTIVE_RECORD_ROW,
        params={"org": org, "ns": namespace, "schema": schema_name, "key": memory_key},
    )
    if row is None:
        return None
    return ActiveRecord(row["rid"], row["mid"], row["record_json"])


async def active_decision_records(
    org: str,
    namespace: str,
    keys: list[str],
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The active decision records under `keys`, one row (memory_key, record_json,
    created_at) each."""
    ch = ch or _default_client()
    params = {"org": org, "ns": namespace, "schema": SCHEMA_NAME, "keys": keys}
    return await ch.query_all(_ACTIVE_DECISION_RECORDS, params=params)


async def live_writer_record(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> WriterRecord | None:
    """The newest active decision record under `memory_key` whose writer still has a live
    decision row there, read with that writer, or None."""
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
