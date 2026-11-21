"""Full-row process registrations, heartbeats and explicit transition acknowledgements."""

from time import time_ns
from typing import Any
from uuid import UUID

from server.core.ch import sequential_reads
from server.core.ch_errors import error_code
from server.core.provider_process import (
    has_certificate,
    participant_proof,
    participant_tag,
    process_identity,
)
from server.core.repo_base import _CHClient, _default_client
from server.core.retirement import RetirementProof, get_gate
from server.settings import settings

TABLE = "loom._provider_participants"
# Heartbeat age is diagnostic; it never establishes permanent retirement.
STALE_AFTER_S = 60

# This conservative certificate subset never hides unknown retirement evidence.
# Bounded ASCII PIDs also exclude proofs the Python integer parser must refuse.
_CERTIFIED_RETIRED = (
    "state = 'retired' AND isValidUTF8(host_proof) "
    "AND length(splitByChar('|', host_proof)) = 9 "
    "AND arrayElement(splitByChar('|', host_proof), 9) = 'cell-retirement-v1' "
    "AND arrayAll(i -> notEmpty(arrayElement(splitByChar('|', host_proof), i)), [1,2,4,5,6]) "
    "AND length(arrayElement(splitByChar('|', host_proof), 5)) <= 20 "
    "AND match(arrayElement(splitByChar('|', host_proof), 5), '^[1-9][0-9]*$') "
    "AND match(arrayElement(splitByChar('|', host_proof), 6), '^[0-9]+$')"
)

# `expired` is decided on the server's clock, like the leases.
PARTICIPANTS_SQL = (
    "SELECT participant_id, transition_id, state, version, heartbeat_at, "
    "host_proof, "
    "heartbeat_at + toIntervalSecond(stale_after_s) < now64(3) AS expired "
    f"FROM {TABLE} FINAL WHERE NOT ({_CERTIFIED_RETIRED})"
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
    f"SELECT participant_id, state, version, host_proof FROM {TABLE} FINAL "
    f"WHERE {_CERTIFIED_RETIRED} "
    "AND heartbeat_at < now64(3) - toIntervalHour({hours:UInt32}) LIMIT 1000"
)
_PRUNE_SQL = f"ALTER TABLE {TABLE} DELETE WHERE participant_id IN {{ids:Array(UUID)}}"


async def participants(
    *, ch: _CHClient | None = None, host_proof: str | None = None
) -> list[dict[str, Any]]:
    with sequential_reads():
        if host_proof is None:
            rows = await (ch or _default_client()).query_all(PARTICIPANTS_SQL)
        else:
            rows = await (ch or _default_client()).query_all(
                PARTICIPANTS_SQL + " AND host_proof = {proof:String} LIMIT 2",
                {"proof": host_proof},
            )
    return [
        r
        for r in rows
        if r["state"] != "retired" or not has_certificate(str(r.get("host_proof") or ""))
    ]


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
    if state not in ("running", "drained"):
        raise ValueError("Unknown participant state")
    own = str(identity) == str(process_identity())
    if not own:
        raise ValueError("A process writes only its own running/drained registration")
    if get_gate().closed:
        raise ValueError("A permanently closed participant cannot re-enroll")
    await (ch or _default_client()).insert(
        TABLE,
        [
            {
                "participant_id": identity,
                "transition_id": transition,
                "state": state,
                "version": version if version is not None else max(time_ns(), after + 1),
                "stale_after_s": STALE_AFTER_S,
                "host_proof": participant_proof(),
            }
        ],
    )


async def record_retirement(proof: RetirementProof, *, ch: _CHClient | None = None) -> None:
    """The sole retired-row writer, passed evidence from the guarded lifecycle."""
    if not isinstance(proof, RetirementProof) or not has_certificate(proof.host_proof):
        raise ValueError("Retirement requires verified process evidence")
    await (ch or _default_client()).insert(
        TABLE,
        [
            {
                "participant_id": proof.participant,
                "transition_id": UUID(int=0),
                "state": "retired",
                "version": max(time_ns(), proof.after + 1, proof.version or 0),
                "stale_after_s": 0,
                "host_proof": proof.host_proof,
            }
        ],
    )


async def prune_retired(*, ch: _CHClient | None = None) -> int:
    """Age selects already certified permanent retirements, never unknown rows.

    Retired UUIDs cannot re-enroll. An earlier in-flight beat is below the final
    version; removing every version cannot resurrect that beat.
    """
    client = ch or _default_client()
    with sequential_reads():
        rows = await client.query_all(_PRUNE_IDS_SQL, {"hours": PRUNE_AFTER_H})
        ids = [r["participant_id"] for r in rows if has_certificate(str(r.get("host_proof") or ""))]
        if not ids:
            return 0
        latest = await client.query_all(
            f"SELECT participant_id, state, version, host_proof FROM {TABLE} FINAL "
            "WHERE participant_id IN {ids:Array(UUID)}",
            {"ids": ids},
        )
        expected = {str(r["participant_id"]): int(r["version"]) for r in rows}
        ids = [
            r["participant_id"]
            for r in latest
            if r["state"] == "retired"
            and int(r["version"]) == expected.get(str(r["participant_id"]))
            and has_certificate(str(r.get("host_proof") or ""))
        ]
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
