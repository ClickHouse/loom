"""Full-row process registrations, heartbeats and explicit transition acknowledgements."""

from time import time_ns
from typing import Any
from uuid import UUID

from server.core.ch import sequential_reads
from server.core.ch_errors import error_code
from server.core.provider_process import host_proof, participant_tag, process_identity
from server.core.repo_base import _CHClient, _default_client
from server.settings import settings

TABLE = "loom._provider_participants"
# A participant whose last heartbeat is older than this may count as stopped.
STALE_AFTER_S = 60

# `expired` is decided on the server's clock, like the leases.
PARTICIPANTS_SQL = (
    "SELECT participant_id, transition_id, state, version, stale_after_s, heartbeat_at, "
    "host_proof, "
    "heartbeat_at + toIntervalSecond(stale_after_s) < now64(3) AS expired "
    f"FROM {TABLE} FINAL WHERE state != 'retired'"
)
ONE_SQL = (
    "SELECT state, version, "
    "heartbeat_at + toIntervalSecond(stale_after_s) < now64(3) AS expired "
    f"FROM {TABLE} FINAL WHERE participant_id = {{id:UUID}}"
)
OWN_VERSION_SQL = f"SELECT max(version) AS v FROM {TABLE} WHERE participant_id = {{id:UUID}}"
SEQUENTIAL = {"select_sequential_consistency": 1}

# Queries still running for one participant, on every node of `ch_cluster`.
_RUNNING = (
    "SELECT count() AS running FROM {source} "
    "WHERE initial_query_id != currentQueryID() AND position(http_user_agent, {{tag:String}}) > 0"
)
RUNNING_LOCAL_SQL = _RUNNING.format(source="system.processes")
# Only inserts: what can still land a write of an older configuration. The
# participant's own heartbeat is not one.
_INSERTS = (
    " AND query_kind = 'Insert' "
    "AND NOT startsWith(Settings['log_comment'], 'provider.participant_heartbeat')"
)
# A server without the cluster answers for itself; any other failure is busy.
CLUSTER_DOESNT_EXIST = 701


def running_sql(*, inserts: bool = False) -> str:
    cluster = settings.ch_cluster
    sql = (
        _RUNNING.format(source=f"clusterAllReplicas('{cluster}', system.processes)")
        if cluster
        else RUNNING_LOCAL_SQL
    )
    return sql + _INSERTS if inserts else sql


# Retired participants whose last row is older than this are deleted.
PRUNE_AFTER_H = 24
_PRUNE_IDS_SQL = (
    f"SELECT participant_id FROM {TABLE} FINAL WHERE state = 'retired' "
    "AND heartbeat_at < now64(3) - toIntervalHour({hours:UInt32}) LIMIT 1000"
)
_PRUNE_SQL = f"ALTER TABLE {TABLE} DELETE WHERE participant_id IN {{ids:Array(UUID)}}"


async def participants(*, ch: _CHClient | None = None) -> list[dict[str, Any]]:
    with sequential_reads():
        return await (ch or _default_client()).query_all(PARTICIPANTS_SQL)


async def record_participant(
    identity: UUID,
    transition: UUID,
    state: str,
    *,
    after: int = 0,
    version: int | None = None,
    ch: _CHClient | None = None,
) -> None:
    """`heartbeat_at` takes the server's clock (its column default); this process's
    own row carries its `host_proof`."""
    if state not in ("running", "drained", "retired"):
        raise ValueError("Unknown participant state")
    own = str(identity) == str(process_identity())
    await (ch or _default_client()).insert(
        TABLE,
        [
            {
                "participant_id": identity,
                "transition_id": transition,
                "state": state,
                "version": version if version is not None else max(time_ns(), after + 1),
                "stale_after_s": STALE_AFTER_S,
                "host_proof": host_proof() if own else "",
            }
        ],
    )


async def prune_retired(*, ch: _CHClient | None = None) -> int:
    """Delete every row of the participants retired `PRUNE_AFTER_H` ago (each
    process start leaves one); a process that was only paused enrolls again
    with its next beat. Returns how many."""
    client = ch or _default_client()
    rows = await client.query_all(_PRUNE_IDS_SQL, {"hours": PRUNE_AFTER_H})
    ids = [r["participant_id"] for r in rows]
    if ids:
        await client.execute(_PRUNE_SQL, {"ids": ids})
    return len(ids)


async def running_queries(
    identity: UUID | str, *, ch: _CHClient | None = None, inserts: bool = False
) -> int | None:
    """None when the count is unknown: the caller treats that as busy. `inserts`
    counts only its running inserts."""
    client = ch or _default_client()
    params = {"tag": participant_tag(identity)}
    local = RUNNING_LOCAL_SQL + _INSERTS if inserts else RUNNING_LOCAL_SQL
    for sql in dict.fromkeys((running_sql(inserts=inserts), local)):
        try:
            row = await client.query_one(sql, params)
        except Exception as e:  # noqa: BLE001 — only a missing cluster asks the local server
            if error_code(e) == CLUSTER_DOESNT_EXIST:
                continue
            return None
        return int(row["running"]) if row is not None and type(row.get("running")) is int else None
    return None


def running_queries_sync(ch: Any, identity: UUID | str) -> int | None:
    """`running_queries` on a clickhouse-connect sync client (the Keeper lock's)."""
    params = {"tag": participant_tag(identity)}
    for sql in dict.fromkeys((running_sql(), RUNNING_LOCAL_SQL)):
        try:
            rows = ch.query(sql, parameters=params).result_rows
        except Exception as e:  # noqa: BLE001 — only a missing cluster asks the local server
            if error_code(e) == CLUSTER_DOESNT_EXIST:
                continue
            return None
        return int(rows[0][0]) if len(rows) == 1 and type(rows[0][0]) is int else None
    return None


def participant_sync(ch: Any, identity: UUID | str) -> dict[str, Any] | None:
    rows = ch.query(ONE_SQL, parameters={"id": str(identity)}, settings=SEQUENTIAL).result_rows
    if not rows:
        return None
    state, version, expired = rows[0]
    return {
        "state": str(state),
        "version": int(version),
        "expired": bool(expired),
    }
