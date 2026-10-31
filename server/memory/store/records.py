"""Writes that close a key's active typed record (supersede, decision tombstone), and the
identity a decision revision writes under."""

from __future__ import annotations

from datetime import datetime

from server.core.datetimes import naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.decision_schema import SCHEMA_NAME
from server.memory.store.sql import _SEQUENTIAL

# The key's active record (params org, ns, schema, key), and the key's live decision rows
# under any identity.
ACTIVE_RECORD = """
FROM loom.memory_records FINAL
WHERE org = {org:String}
  AND namespace = {ns:String}
  AND schema_name = {schema:String}
  AND memory_key = {key:String}
  AND status = 'active'
  AND tombstoned_at IS NULL
"""
_LIVE_DECISION_ROWS = """
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
{ACTIVE_RECORD}"""

_TOMBSTONE_DECISION_RECORD = f"""{_CLOSE_ACTIVE_RECORD}
  AND (SELECT count() {_LIVE_DECISION_ROWS}) = 0
{_SEQUENTIAL}"""

_LIVE_DECISION_IDENTITY = f"""
SELECT agent, user_id, session_id
{_LIVE_DECISION_ROWS}
ORDER BY (agent, user_id, session_id) IN (
    SELECT identity_agent, user_id, session_id {ACTIVE_RECORD}
) DESC, version DESC
LIMIT 1
{_SEQUENTIAL}"""


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
    """Tombstone the decision record under `memory_key` when no live decision row holds
    the key. `version` is the lowest version the tombstone is written at.

    The record plane keys by (schema_name, memory_key) with no identity, while loom.memory
    keys by identity, so the record stays while any live decision row holds the key:
    another identity's, or a write that landed above the tombstone. The reads are
    sequential, so they see the delete's own tombstone and every row committed before
    it, on any replica."""
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


async def live_decision_identity(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> tuple[str, str, str] | None:
    """The (agent, user_id, session_id) of a live decision row under `memory_key`: the
    active record's writer when its row is live, else the newest live row. None when no
    live decision row holds the key."""
    ch = ch or _default_client()
    row = await ch.query_one(
        _LIVE_DECISION_IDENTITY,
        params={
            "org": org,
            "ns": namespace,
            "schema": SCHEMA_NAME,
            "key": memory_key,
        },
    )
    return None if row is None else (row["agent"], row["user_id"], row["session_id"])
