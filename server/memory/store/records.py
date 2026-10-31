"""Version rows that close the active typed record under a key in loom.memory_records:
supersession by a newer record, and the tombstone of a decision whose memory row was
deleted."""

from __future__ import annotations

from datetime import datetime

from server.core.datetimes import naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client

# The built-in schema and memory type decision.record writes (server.memory.app.decisions).
DECISION = "decision"

# Copies the key's active record with a new status, a closed validity interval and a
# version above its own. The computed columns take new names: `AS status` would shadow
# the column and turn `status = 'active'` below into a constant (see
# server/code/store/index/edges.py live_edges_for_path). INSERT..SELECT maps by position.
_CLOSE_ACTIVE_RECORD = """
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
    {now:DateTime} AS new_updated_at,
    {tombstoned_at:Nullable(DateTime)} AS new_tombstoned_at
FROM loom.memory_records FINAL
WHERE org = {org:String}
  AND namespace = {ns:String}
  AND schema_name = {schema:String}
  AND memory_key = {key:String}
  AND status = 'active'
  AND tombstoned_at IS NULL
"""

# No live decision row holds the key, under any identity.
_NO_LIVE_DECISION = """
  AND (
      SELECT count()
      FROM loom.memory FINAL
      WHERE org = {org:String}
        AND namespace = {ns:String}
        AND memory_key = {key:String}
        AND memory_type = {memory_type:String}
        AND tombstoned_at IS NULL
  ) = 0
"""


async def supersede_active_record(
    ch: _CHClient,
    org: str,
    namespace: str,
    schema_name: str,
    memory_key: str,
    *,
    close_at: datetime,
    version: int,
    now: datetime,
) -> None:
    """Close the key's active record ahead of a new one written at `version`."""
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
    """Tombstone the decision record under `memory_key` once a tombstone at `version`
    has removed a decision row there.

    The record plane keys by (schema_name, memory_key) with no identity, while loom.memory
    keys by identity, so the record stays while any live decision row holds the key:
    another identity's, or a write that landed above the tombstone. The reads are
    sequential, so they see the delete's own tombstone and every row committed before
    it, on any replica."""
    ch = ch or _default_client()
    now = naive_utc_now_seconds()
    await ch.execute(
        _CLOSE_ACTIVE_RECORD + _NO_LIVE_DECISION,
        params={
            "org": org,
            "ns": namespace,
            "schema": DECISION,
            "memory_type": DECISION,
            "key": memory_key,
            "status": "tombstoned",
            "close": now,
            "ver": version,
            "now": now,
            "tombstoned_at": now,
        },
        settings={"select_sequential_consistency": 1},
    )
