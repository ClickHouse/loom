"""Full-row process registrations, heartbeats and explicit transition acknowledgements."""

from dataclasses import dataclass
from time import time_ns
from typing import Any, Literal, overload
from uuid import UUID

from server.core import cell_sync, ch_names
from server.core.cell_ch import (
    TRANSACTION_S,
    WHOLE_SQL,
    Refused,
    Topology,
    complete,
    per_host,
    quote,
    read_topology,
)
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
from server.insights.domain.cell_participants import bounded_participant, latest, refused
from server.insights.store import cell_catalog
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


@dataclass(frozen=True)
class ParticipantCensus:
    rows: dict[str, dict[str, Any]]
    topology: Topology
    names: dict[str, str]
    engine: str


@overload
async def census(ch: Any, *, bounded: Literal[False] = False) -> dict[str, dict[str, Any]]: ...


@overload
async def census(ch: Any, *, bounded: Literal[True]) -> ParticipantCensus: ...


async def census(
    ch: Any, *, bounded: bool = False
) -> dict[str, dict[str, Any]] | ParticipantCensus:
    """Synchronize after the durable cutoff; every configured replica must answer."""
    topology = await read_topology(ch, max_replicas=64) if bounded else await read_topology(ch)
    if topology.cluster and not topology.entries:
        raise refused("participant census cannot resolve the configured cluster")
    if problems := topology.refusals():
        raise Refused(problems)
    names = await cell_catalog.host_names(ch, topology) if topology.multi else {}
    if bounded and topology.cluster and not topology.multi:
        names = await bounded_local_replica(ch, topology)
    if problems := (await cell_catalog.own_refusals(ch, topology, names))[0]:
        raise Refused(problems)
    database = ch_names.database()
    tables = await per_host(
        ch,
        topology,
        names,
        (
            "SELECT hostName() AS h, engine FROM {source:tables} WHERE database={db:String} AND "
            "name='_provider_participants'"
        ),
        {"db": database},
    )
    if not tables or any(len(rows) != 1 for rows in tables.values()):
        raise refused("participant table is missing from a configured replica")
    engines = {str(rows[0]["engine"]) for rows in tables.values()}
    if len(engines) != 1 or not next(iter(engines)).endswith("MergeTree"):
        raise refused("participant replicas have unreadable or different table engines")
    engine = next(iter(engines))
    if bounded and engine not in (
        "ReplacingMergeTree",
        "ReplicatedReplacingMergeTree",
        "SharedReplacingMergeTree",
    ):
        raise refused("participant table has an unsupported replacement policy")
    if engine.startswith("Replicated"):
        replica_rows = await per_host(
            ch,
            topology,
            names,
            (
                "SELECT hostName() AS h, database, table, zookeeper_path, replica_name, "
                "replica_is_active FROM {source:replicas} WHERE database={db:String} AND "
                "table='_provider_participants'"
            ),
            {"db": database},
        )
        if any(len(value) != 1 for value in replica_rows.values()) or cell_catalog.extra_replicas(
            row for value in replica_rows.values() for row in value
        ):
            raise refused("participant replica set includes an unverified replica")
        if bounded:
            bounded_replica_paths(replica_rows)
        budget = cell_sync.Budget(TRANSACTION_S)
        await cell_sync._system(
            ch,
            f"SYSTEM SYNC REPLICA{cell_sync._on_cluster(topology)} "
            f"{quote(database)}.`_provider_participants` LIGHTWEIGHT",
            topology,
            budget,
            "participant census",
        )
    source = f"{quote(database)}.`_provider_participants`"
    params = {}
    if topology.multi:
        source = f"clusterAllReplicas({{cluster:String}}, {source})"
        params["cluster"] = topology.cluster
    sql = f"SELECT participant_id, state, version, host_proof FROM {source} FINAL"
    if bounded:
        sql = (
            f"SELECT hostName() AS h, participant_id, state, version, "
            "if(length(census_participant.host_proof) <= 2048, "
            "census_participant.host_proof, '') AS host_proof, "
            f"length(census_participant.host_proof) AS proof_bytes "
            f"FROM {source} AS census_participant LIMIT 1025 BY h"
        )
    rows = await ch.query_all(complete(sql, sequential=int(engine.startswith("Shared"))), params)
    if bounded:
        rows = bounded_participants(rows, topology, names)
        return ParticipantCensus(latest(rows), topology, names, engine)
    return latest(rows)


def bounded_participants(
    rows: list[dict[str, Any]], topology: Any, names: Any
) -> list[dict[str, Any]]:
    """Validate replica duplication and cap+1 before the shared latest selector."""
    counts: dict[str, int] = {}
    logical: set[str] = set()
    out = []
    for row in rows:
        host = row.get("h")
        if not isinstance(host, str) or not host or len(host.encode()) > 2048:
            raise refused("participant row has an unreadable replica")
        if topology.multi and host not in names:
            raise refused("participant row came from an unconfigured replica")
        if host not in counts and len(counts) >= (len(names) if topology.multi else 1):
            raise refused("participant row came from an extra replica")
        count = counts.get(host, 0) + 1
        if count > 1024:
            raise refused("participant replica row limit exceeded")
        counts[host] = count
        if type(row.get("proof_bytes")) is not int or not 0 <= row["proof_bytes"] <= 2048:
            raise refused("participant proof exceeds its field bound")
        if (
            not isinstance(row.get("host_proof"), str)
            or len(row["host_proof"].encode()) != row["proof_bytes"]
        ):
            raise refused("participant proof length disagrees with its projection")
        value = bounded_participant(
            {key: row[key] for key in ("participant_id", "state", "version", "host_proof")}
        )
        identity = value["participant_id"]
        if identity not in logical and len(logical) >= 1024:
            raise refused("logical participant limit exceeded")
        logical.add(identity)
        out.append(value)
    return out


def bounded_replica_paths(rows: dict[str, list[dict[str, Any]]]) -> None:
    """All replicas synchronize one table identity, not unrelated same-named tables."""
    paths: set[str] = set()
    names: set[str] = set()
    for values in rows.values():
        row = values[0]
        path, name = row.get("zookeeper_path"), row.get("replica_name")
        if (
            not isinstance(path, str)
            or not path
            or len(path.encode()) > 2048
            or not isinstance(name, str)
            or not name
            or len(name.encode()) > 2048
            or name in names
        ):
            raise refused("participant replica identity is unreadable or duplicated")
        paths.add(path)
        names.add(name)
    if len(paths) != 1:
        raise refused("participant replicas do not share one table identity")
    for values in rows.values():
        listed = values[0].get("replica_is_active")
        if (
            not isinstance(listed, dict)
            or set(listed) != names
            or any(type(v) is not int or v != 1 for v in listed.values())
        ):
            raise refused("participant replica membership is missing, inactive or unreadable")


async def bounded_local_replica(ch: Any, topology: Topology) -> dict[str, str]:
    """A configured one-host cluster must describe the server read locally."""
    names = await cell_catalog.host_names(ch, topology)
    local = await ch.query_one("SELECT hostName() AS h " + WHOLE_SQL)
    if local is None or local.get("h") not in names:
        raise refused("local participant reader is outside the configured one-host cluster")
    return names
