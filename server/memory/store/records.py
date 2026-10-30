"""Version rows that close the active typed record under a key in loom.memory_records:
supersession by a newer record, and the tombstone of a decision whose memory row was
deleted."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client

# The built-in schema and memory type decision.record writes (server.memory.app.decisions).
DECISION = "decision"

# Copies the key's active record with a new status, a closed validity interval and a
# version above its own. The computed columns take new names: `AS status` would shadow
# the column and turn `status = 'active'` below into a constant (see
# server/code/store/index/edges.py live_edges_for_path). INSERT..SELECT maps by position.
# `{guard}` is a further condition from this module, never caller text.
_CLOSE_ACTIVE_RECORD = """
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
    {{now:DateTime}} AS new_updated_at,
    {{tombstoned_at:Nullable(DateTime)}} AS new_tombstoned_at
FROM loom.memory_records FINAL
WHERE org = {{org:String}}
  AND namespace = {{ns:String}}
  AND schema_name = {{schema:String}}
  AND memory_key = {{key:String}}
  AND status = 'active'
  AND tombstoned_at IS NULL
  {guard}
"""

# No live decision row under the key outside the deleted row's own identity. The deleted
# row is left out by name, so a read that has not seen its tombstone does not count it.
_NO_OTHER_LIVE_DECISION = """
  AND (
      SELECT count()
      FROM loom.memory FINAL
      WHERE org = {org:String}
        AND namespace = {ns:String}
        AND memory_key = {key:String}
        AND memory_type = {schema:String}
        AND tombstoned_at IS NULL
        AND (agent, user_id, session_id) != ({agent:String}, {uid:String}, {sid:String})
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
        _CLOSE_ACTIVE_RECORD.format(guard=""),
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
    deleted: Any, *, version: int, ch: _CHClient | None = None
) -> None:
    """Tombstone the decision record under the key of `deleted`, a decision row that a
    tombstone at `version` just removed.

    The record plane keys by (schema_name, memory_key) with no identity, while loom.memory
    keys by identity, so the record stays while another identity still holds a live
    decision row under the key: retiring it would hide that row's record too. The reads
    are sequential, so two deletes from two identities each see the other's tombstone
    once it is written, and a lagging replica cannot miss a live row."""
    ch = ch or _default_client()
    now = naive_utc_now_seconds()
    await ch.execute(
        _CLOSE_ACTIVE_RECORD.format(guard=_NO_OTHER_LIVE_DECISION),
        params={
            "org": deleted.org,
            "ns": deleted.namespace,
            "schema": DECISION,
            "key": deleted.memory_key,
            "agent": deleted.agent,
            "uid": deleted.user_id,
            "sid": deleted.session_id,
            "status": "tombstoned",
            "close": now,
            "ver": version,
            "now": now,
            "tombstoned_at": now,
        },
        settings={"select_sequential_consistency": 1},
    )
