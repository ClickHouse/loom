"""Active typed records of loom.memory_records: reads, and the supersede and decision
tombstone copies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

from server.core import pg
from server.core.ch import late_send, start_guard, write_limit_s
from server.core.datetimes import naive_utc_now_seconds
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.decision_schema import PROPOSED_TAG, SCHEMA_NAME
from server.memory.store.sql import _SEQUENTIAL

# The active records of a schema.
_ACTIVE = """org = {org:String}
  AND namespace = {ns:String}
  AND schema_name = {schema:String}
  AND status = 'active'
  AND tombstoned_at IS NULL"""

# The FROM and WHERE both writer sets below share: each key's live decision rows.
_LIVE_DECISION_ROWS = """
    FROM loom.memory FINAL
    WHERE org = {org:String}
      AND namespace = {ns:String}
      AND memory_key IN {keys:Array(String)}
      AND memory_type = 'decision'
      AND tombstoned_at IS NULL"""

# Each key's identities with a live decision row under it.
_LIVE_WRITERS = f"""
    SELECT memory_key, agent, user_id, session_id{_LIVE_DECISION_ROWS}
  """

# Each key's identities whose live decision row is tagged decision:proposed.
_PROPOSED_WRITERS = f"""
    SELECT memory_key, agent, user_id, session_id{_LIVE_DECISION_ROWS}
      AND has(tags, {{proposed:String}})
  """

# Computed columns take new names: `AS status` would shadow the column in the WHERE.
_COPY_ACTIVE_RECORDS = f"""
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
FROM loom.memory_records FINAL
WHERE {_ACTIVE}"""

_CLOSE_ACTIVE_RECORD = f"""{_COPY_ACTIVE_RECORDS}
  AND memory_key = {{key:String}}"""

# Retires each key's record unless its writer still has a live decision row under the key.
_TOMBSTONE_DECISION_RECORDS = f"""{_COPY_ACTIVE_RECORDS}
  AND memory_key IN {{keys:Array(String)}}
  AND (memory_key, identity_agent, user_id, session_id) NOT IN ({_LIVE_WRITERS})
{_SEQUENTIAL}"""

# The same against the live writers the Postgres authority holds, read apart from the copy:
# only records below a version drawn before that read, and starting by `start_by` (server
# ms) like a carried insert, so it never lands after an erasure waited out its publisher.
_TOMBSTONE_DECISION_RECORDS_EXCEPT_WRITERS = f"""{_COPY_ACTIVE_RECORDS}
  AND memory_key IN {{keys:Array(String)}}
  AND (memory_key, identity_agent, user_id, session_id)
      NOT IN {{live:Array(Tuple(String, String, String, String))}}
  AND version < {{ver:UInt64}}
  AND {start_guard("{start_by:Int64}")}
{_SEQUENTIAL}"""


def _live_writer_record(writers: str) -> str:
    """The newest active record whose writer is among `writers` under the key."""
    return f"""
SELECT identity_agent, user_id, session_id, record_json, created_at
FROM loom.memory_records FINAL
WHERE {_ACTIVE}
  AND memory_key IN {{keys:Array(String)}}
  AND (memory_key, identity_agent, user_id, session_id) IN {writers}
ORDER BY version DESC
LIMIT 1
{_SEQUENTIAL}"""


# The writers with a live decision row: in ClickHouse, or the heads the Postgres
# authority holds, which ClickHouse may not have yet.
_LIVE_WRITER_RECORD = _live_writer_record(f"({_LIVE_WRITERS})")
_LIVE_WRITER_RECORD_AMONG = _live_writer_record(
    "{live:Array(Tuple(String, String, String, String))}"
)

# The key's newest active record.
_ACTIVE_RECORD_ROW = f"""
SELECT toString(record_id) AS rid, toString(memory_id) AS mid, record_json
FROM loom.memory_records FINAL
WHERE {_ACTIVE}
  AND memory_key = {{key:String}}
ORDER BY version DESC
LIMIT 1"""

# The active decision records of several keys, each with whether its writer's live row
# records it as proposed: the stage is on the row, not the record.
_ACTIVE_DECISION_RECORDS = f"""
SELECT memory_key, record_json, created_at,
  (memory_key, identity_agent, user_id, session_id) IN ({_PROPOSED_WRITERS}) AS proposed
FROM loom.memory_records FINAL
WHERE {_ACTIVE}
  AND memory_key IN {{keys:Array(String)}}"""


class ActiveRecord(NamedTuple):
    """The key's newest active record, as the record write's fold reads it."""

    record_id: str
    memory_id: str | None
    record_json: str


class DecisionRecord(NamedTuple):
    """An active decision record of one key; `proposed` when its writer's live row is
    tagged decision:proposed."""

    memory_key: str
    record_json: str
    created_at: datetime
    proposed: bool


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


async def tombstone_decision_records(
    org: str,
    namespace: str,
    keys: list[str],
    *,
    version: int,
    ch: _CHClient | None = None,
    live: list[tuple[str, str, str, str]] | None = None,
) -> None:
    """Tombstone each key's active decision record unless its writer still has a live
    decision row there, in one statement. Stamps `tombstoned_at` and the closed
    `valid_to` with the current time.

    Under Postgres the live writers are the heads the authority holds (`live` when the
    caller read them), and only records below `version` are copied."""
    ch = ch or _default_client()
    now = naive_utc_now_seconds()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "schema": SCHEMA_NAME,
        "keys": keys,
        "status": "tombstoned",
        "close": now,
        "ver": version,
        "now": now,
        "tombstoned_at": now,
    }
    if not pg.enabled():
        await ch.execute(_TOMBSTONE_DECISION_RECORDS, params=params)
        return
    from server.core import rmt_carry
    from server.memory.store import pg_memory

    if live is None:
        live = await pg_memory.live_decision_writers(org, namespace, keys)
    params["live"] = live
    params["start_by"] = round(await rmt_carry.measured_send_deadline(ch) * 1000)
    try:
        await ch.execute(
            _TOMBSTONE_DECISION_RECORDS_EXCEPT_WRITERS,
            params=params,
            # Bounded as `pg.insert_bound_s` counts it, a zero write timeout included.
            settings={"max_execution_time": write_limit_s()},
        )
    except Exception as e:
        late = late_send(e, "a decision record retirement")
        if late is None:
            raise
        raise late from None


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
) -> list[DecisionRecord]:
    """The active decision records under `keys`."""
    ch = ch or _default_client()
    params = {
        "org": org,
        "ns": namespace,
        "schema": SCHEMA_NAME,
        "keys": keys,
        "proposed": PROPOSED_TAG,
    }
    rows = await ch.query_all(_ACTIVE_DECISION_RECORDS, params=params)
    return [
        DecisionRecord(r["memory_key"], r["record_json"], r["created_at"], bool(r["proposed"]))
        for r in rows
    ]


async def live_writer_record(
    org: str,
    namespace: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> WriterRecord | None:
    """The newest active decision record under `memory_key` whose writer still has a live
    decision row there (under Postgres, a head the authority holds), read with that
    writer, or None."""
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "schema": SCHEMA_NAME,
        "keys": [memory_key],
    }
    sql = _LIVE_WRITER_RECORD
    if pg.enabled():
        from server.memory.store import pg_memory

        params["live"] = await pg_memory.live_decision_writers(org, namespace, [memory_key])
        if not params["live"]:
            return None
        sql = _LIVE_WRITER_RECORD_AMONG
    row = await ch.query_one(sql, params=params)
    if row is None:
        return None
    return WriterRecord(
        row["identity_agent"],
        row["user_id"],
        row["session_id"],
        row["record_json"],
        row["created_at"],
    )
