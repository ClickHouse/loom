"""A post-cutoff participant census and permanent retirement barrier for update."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any, cast
from uuid import UUID

from server.cli import cell_control, cell_scan, cell_writers
from server.cli.cell_ch import WHOLE_SQL, Refused, per_host, quote, read_topology
from server.core import cell, ch_names, instances
from server.core.ch import query_tag
from server.core.provider_process import (
    certified,
    has_certificate,
    participant_tag,
    proves_stopped,
)
from server.insights.store import provider_participants as participants


class CompleteReads:
    """Pass complete uncached read settings to the existing instance reader."""

    def __init__(self, ch: Any) -> None:
        self.ch = ch

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            await self.ch.query_all(complete(sql, sequential=1), params or {}),
        )


def complete(sql: str, *, sequential: int = 0) -> str:
    return (
        sql
        + " "
        + WHOLE_SQL
        + ", skip_unavailable_shards=0, prefer_localhost_replica=1, "
        + f"select_sequential_consistency={sequential}"
    )


def latest(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    # The latest winner cannot hide a conflicting older version from another replica.
    seen: dict[tuple[str, int], tuple[str, str]] = {}
    for row in rows:
        try:
            identity = str(UUID(str(row["participant_id"])))
            version = row["version"]
            state, proof = row["state"], row["host_proof"]
        except (KeyError, TypeError, ValueError):
            raise cell_control._refused("participant census contains an unreadable row") from None
        if (
            type(version) is not int
            or version <= 0
            or state not in ("running", "drained", "retired")
            or not isinstance(proof, str)
        ):
            raise cell_control._refused("participant census contains an unreadable row")
        previous = out.get(identity)
        observed = seen.get((identity, version))
        if observed is not None and observed != (state, proof):
            raise cell_control._refused(f"participant {identity} has conflicting equal versions")
        seen[(identity, version)] = (state, proof)
        if previous is None or int(previous["version"]) < version:
            out[identity] = dict(row)
    return out


def capture(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        identity: row
        for identity, row in latest(rows).items()
        if row["state"] != "retired" or not has_certificate(row["host_proof"])
    }


async def census(ch: Any) -> dict[str, dict[str, Any]]:
    """Synchronize after the durable cutoff; every configured replica must answer."""
    topology = await read_topology(ch)
    if topology.cluster and not topology.entries:
        raise cell_control._refused("participant census cannot resolve the configured cluster")
    if problems := topology.refusals():
        raise Refused(problems)
    names = await cell_writers.host_names(ch, topology) if topology.multi else {}
    if problems := (await cell_writers.own_refusals(ch, topology, names))[0]:
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
        raise cell_control._refused("participant table is missing from a configured replica")
    engines = {str(rows[0]["engine"]) for rows in tables.values()}
    if len(engines) != 1 or not next(iter(engines)).endswith("MergeTree"):
        raise cell_control._refused(
            "participant replicas have unreadable or different table engines"
        )
    engine = next(iter(engines))
    if engine.startswith("Replicated"):
        rows = await per_host(
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
        if any(len(value) != 1 for value in rows.values()) or cell_writers.extra_replicas(
            row for value in rows.values() for row in value
        ):
            raise cell_control._refused("participant replica set includes an unverified replica")
        budget = cell_scan.Budget(cell_control.TRANSACTION_S)
        await cell_scan._system(
            ch,
            f"SYSTEM SYNC REPLICA{cell_scan._on_cluster(topology)} "
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
    rows = await ch.query_all(complete(sql, sequential=int(engine.startswith("Shared"))), params)
    return latest(rows)


async def stopped(ch: Any, initial: dict[str, Any], current: dict[str, Any] | None) -> bool:
    if current is not None:
        try:
            same_identity = certified(initial["host_proof"]) == current["host_proof"]
        except ValueError:
            same_identity = False
        if (
            current["state"] == "retired"
            and has_certificate(current["host_proof"])
            and same_identity
            and int(current["version"]) > int(initial["version"])
        ):
            return True
    if current is not None:
        try:
            if certified(initial["host_proof"]) != certified(current["host_proof"]):
                return False
        except ValueError:
            if initial["host_proof"] != current["host_proof"]:
                return False
    if not proves_stopped(initial["host_proof"]):
        return False
    # Never use the model protocol's missing-cluster local fallback as death proof.
    row = await ch.query_one(
        complete(participants.running_sql()),
        {"tag": participant_tag(initial["participant_id"])},
    )
    return row is not None and type(row.get("running")) is int and row["running"] == 0


async def replacement(
    ch: Any, run: Any, rows: dict[str, dict[str, Any]], captured: set[str], target: dict[str, Any]
) -> bool:
    reader = CompleteReads(ch)
    live = await instances.read_live(reader, all_deployments=True)
    reported = await reader.query_all(
        f"SELECT instance_id, cell, tenancy_mode, fingerprint FROM {instances.CELL_TABLE} "
        "WHERE has({ids:Array(String)}, instance_id)",
        {"ids": [str(row["instance_id"]) for row in live]},
    )
    by_instance = {str(row["instance_id"]): row for row in reported}
    for instance in live:
        beat = by_instance.get(str(instance["instance_id"]))
        if not beat or not instance["serving"] or beat["cell"] != run.args.cell:
            continue
        try:
            value = json.loads(beat["fingerprint"])
            identity = str(UUID(value["participant_id"]))
        except (ValueError, TypeError, KeyError):
            continue
        participant = rows.get(identity)
        if (
            identity in captured
            or participant is None
            or participant["state"] == "retired"
            or not has_certificate(participant["host_proof"])
        ):
            continue
        if (
            value.get("serves") is not True
            or value.get("marked") is not True
            or (run.row["is_first"] and value.get("router") is not True)
        ):
            continue
        if beat["tenancy_mode"] not in ("recording", "enforced") or not isinstance(
            value.get("fields"), dict
        ):
            continue
        if cell.matches(value["fields"], target):
            return True
    return False


async def settle(run: Any, target: dict[str, Any]) -> None:
    """Fix one complete captured set; expiry never removes an obligation."""
    with query_tag("cell.update.participants"):
        initial = await run.owner.call(lambda: census(run.ch), total_s=cell_control.TRANSACTION_S)
        captured = capture(list(initial.values()))
        done: set[str] = set()
        deadline = time.monotonic() + run.args.wait
        while True:
            await cell_control.check_lock(run.owner.conn)

            async def poll() -> bool:
                rows = await census(run.ch)
                for identity, original in captured.items():
                    if identity not in done and await stopped(run.ch, original, rows.get(identity)):
                        done.add(identity)
                return await replacement(run.ch, run, rows, set(captured), target)

            live = await run.owner.call(poll, total_s=cell_control.TRANSACTION_S)
            remaining = sorted(set(captured) - done)
            if not remaining and live:
                return
            if time.monotonic() >= deadline:
                blocked = ", ".join(remaining) or "no captured participant remains"
                raise cell_control._refused(
                    f"participant turnover incomplete ({blocked}); "
                    f"serving target replacement={live}. "
                    "Directory stays dual and local acceptance stays single. Online finalization "
                    "requires foundation code and positive legacy-process turnover; this census "
                    "cannot verify that external inventory"
                )
            print(
                f"loom cell update: waiting for participants {', '.join(remaining) or 'none'}; "
                f"serving target replacement={live}",
                file=sys.stderr,
            )
            await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
