"""Complete bounded process census for the explicit later-attach component."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from server.core import cell, ch_names
from server.core.cell_ch import WHOLE, Topology, complete, per_host, quote
from server.core.ch_bounded import BoundedReadError, decode_json, read_rows
from server.insights.domain import cell_observations as domain
from server.insights.domain.cell_participants import refused
from server.insights.store import provider_participants

TAG = "cell.attach.census"
_TABLES = ("_provider_participants", "cell_heartbeats", "instances")
_COLUMNS = {
    "_provider_participants": {
        "participant_id": "UUID",
        "state": "LowCardinality(String)",
        "version": "UInt64",
        "host_proof": "String",
    },
    "cell_heartbeats": {
        "instance_id": "String",
        "cell": "String",
        "tenancy_mode": "String",
        "fingerprint": "String",
        "heartbeat_at": "DateTime64(3)",
    },
    "instances": {
        "instance_id": "String",
        "keys_fp": "String",
        "serving": "UInt8",
        "stopped": "UInt8",
        "heartbeat_at": "DateTime64(3)",
    },
}


class _Reads:
    """One complete census shares one raw allowance across all reads and passes."""

    def __init__(self, ch: Any, attempt: domain.Attempt, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if not math.isfinite(deadline) or not 0 < remaining <= 7200:
            raise refused("census has no finite remaining workflow allowance")
        if attempt.deadline is not None and deadline != attempt.deadline:
            raise refused("census cannot reset its workflow deadline")
        attempt.deadline = deadline
        self.ch, self.deadline = ch, deadline
        self.raw_bytes_used = 0

    def left(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise refused("census deadline exhausted")
        return remaining

    @asynccontextmanager
    async def raw_stream(
        self, sql: str, params: Any = None, *, settings: Any = None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        effective = {
            **WHOLE,
            "skip_unavailable_shards": 0,
            "prefer_localhost_replica": 1,
            "default_format": "JSONEachRow",
            **(settings or {}),
        }
        remaining = self.left()
        for name in ("max_execution_time", "receive_timeout", "distributed_ddl_task_timeout"):
            if name in effective:
                effective[name] = min(float(effective[name]), remaining)
        async with asyncio.timeout(remaining):
            async with self.ch.raw_stream(sql, params=params or {}, settings=effective) as chunks:

                async def limited() -> AsyncIterator[bytes]:
                    async for chunk in chunks:
                        if self.raw_bytes_used + len(chunk) > domain.METADATA_BYTES:
                            raise BoundedReadError("Aggregate census raw byte limit exceeded")
                        self.raw_bytes_used += len(chunk)
                        yield chunk

                yield limited()

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        values = params or {}
        # A JSONEachRow object needs at least three bytes: {} followed by newline.
        max_rows = domain.METADATA_BYTES // 3
        if "replica_limit" in values:
            max_rows = domain.REPLICAS + 1
        if "LIMIT 1025 BY h" in sql:
            max_rows = (domain.PARTICIPANTS + 1) * domain.REPLICAS
        result = await read_rows(
            sql,
            values,
            ch=self,
            deadline=self.deadline,
            row_bytes=domain.FINGERPRINT_BYTES + 32 * domain.FIELD_BYTES,
            total_bytes=domain.METADATA_BYTES - self.raw_bytes_used,
            max_rows=max_rows,
            tag=TAG,
        )
        self.left()
        _metadata_fields(result)
        if "groupArrayIf(host_name, is_local)" in sql:
            _host_names(result)
        return result

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = await self.query_all(sql, params)
        if len(rows) > 1:
            raise refused("single-row census query returned more than one row")
        return rows[0] if rows else None

    async def raw_bytes(self, sql: str, params: Any = None, *, settings: Any = None) -> bytes:
        chunks = []
        async with self.raw_stream(sql, params, settings=settings) as stream:
            async for chunk in stream:
                chunks.append(chunk)
        body = b"".join(chunks)
        lines = body.splitlines()
        if len(lines) > domain.REPLICAS:
            raise refused("replica synchronization status limit exceeded")
        for line in lines:
            if len(line) > domain.FINGERPRINT_BYTES:
                raise refused("replica synchronization status row exceeds its bound")
            value = decode_json(line.decode("utf-8"))
            if not isinstance(value, dict):
                raise refused("unreadable replica synchronization status")
            _metadata_fields([value])
        return body

    async def execute(self, sql: str, params: Any = None, *, settings: Any = None) -> None:
        remaining = self.left()
        bounds = dict(settings or {})
        for name in ("max_execution_time", "receive_timeout"):
            bounds[name] = min(float(bounds.get(name, remaining)), remaining)
        async with asyncio.timeout(remaining):
            await self.ch.execute(sql, params, settings=bounds)


def _metadata_fields(rows: list[dict[str, Any]]) -> None:
    # Fingerprint has its own larger bound and strict nested decoder before retention.
    for row in rows:
        for key, value in row.items():
            domain.text(key)
            if key != "fingerprint":
                pending = [value]
                while pending:
                    item = pending.pop()
                    if isinstance(item, str):
                        domain.text(item, empty=True)
                    elif isinstance(item, list):
                        pending.extend(item)
                    elif isinstance(item, dict):
                        for name, child in item.items():
                            domain.text(name)
                            pending.append(child)


def _host_names(rows: list[dict[str, Any]]) -> None:
    if len(rows) > domain.REPLICAS:
        raise refused("answering replica limit exceeded")
    for row in rows:
        domain.text(row.get("h"))
        names = row.get("names")
        if not isinstance(names, list) or len(names) != 1:
            raise refused("answering replica has no unique configured name")
        domain.text(names[0])


async def _catalog(ch: _Reads, topology: Topology, names: dict[str, str]) -> None:
    params = {"db": ch_names.database(), "tables": list(_TABLES)}
    tables = await per_host(
        ch,
        topology,
        names,
        "SELECT hostName() AS h, name, engine, engine_full FROM {source:tables} "
        "WHERE database={db:String} AND name IN {tables:Array(String)}",
        params,
    )
    columns = await per_host(
        ch,
        topology,
        names,
        "SELECT hostName() AS h, table, name, type FROM {source:columns} "
        "WHERE database={db:String} AND table IN {tables:Array(String)}",
        params,
    )
    reference = None
    for host, found in tables.items():
        if len(found) != len(_TABLES) or len({r.get("name") for r in found}) != len(_TABLES):
            raise refused("census table catalog is missing or duplicated")
        kinds = {domain.text(r["name"]): domain.text(r["engine"]) for r in found}
        if kinds.get("cell_heartbeats") != "KeeperMap" or kinds.get("instances") != "KeeperMap":
            raise refused("observation or instance table is not KeeperMap")
        typed = tuple(
            sorted(
                (domain.text(r["table"]), domain.text(r["name"]), domain.text(r["type"]))
                for r in columns.get(host, [])
            )
        )
        if len({(t, n) for t, n, _ in typed}) != len(typed):
            raise refused("census column catalog is duplicated")
        for table, expected in _COLUMNS.items():
            actual = {n: kind for t, n, kind in typed if t == table}
            if any(actual.get(name) != kind for name, kind in expected.items()):
                raise refused("census table has an unsupported column type")
        keeper = tuple(
            sorted(
                (r["name"], domain.text(r["engine_full"]))
                for r in found
                if r["name"] != "_provider_participants"
            )
        )
        view = (tuple(sorted(kinds.items())), typed, keeper)
        if reference is not None and reference != view:
            raise refused("census replicas disagree on their table catalog")
        reference = view


def _source(table: str, topology: Topology) -> tuple[str, dict[str, Any]]:
    source = f"{quote(ch_names.database())}.`{table}`"
    if topology.multi:
        return f"clusterAllReplicas({{cluster:String}}, {source})", {"cluster": topology.cluster}
    return source, {}


async def _observations(ch: _Reads, topology: Topology) -> list[dict[str, Any]]:
    source, params = _source("cell_heartbeats", topology)
    sql = (
        "SELECT hostName() AS h, "
        "if(length(census_observation.instance_id)<=2048, "
        "census_observation.instance_id, '') AS instance_id, "
        "if(length(census_observation.cell)<=2048, census_observation.cell, '') AS "
        "cell, length(census_observation.cell) AS cell_bytes, "
        "if(length(census_observation.tenancy_mode)<=2048, "
        "census_observation.tenancy_mode, '') AS tenancy_mode, "
        "length(census_observation.tenancy_mode) AS tenancy_mode_bytes, "
        "if(length(census_observation.fingerprint)<=262144, "
        "census_observation.fingerprint, '') AS fingerprint, "
        "length(census_observation.fingerprint) AS fingerprint_bytes, "
        "toUnixTimestamp64Milli(census_observation.heartbeat_at) AS generation, "
        "dateDiff('millisecond', census_observation.heartbeat_at, now64(3)) AS age_ms "
        f"FROM {source} AS census_observation LIMIT 1025 BY h"
    )
    return await ch.query_all(complete(sql, sequential=1), params)


async def _instances(ch: _Reads, topology: Topology) -> list[dict[str, Any]]:
    source, params = _source("instances", topology)
    sql = (
        "SELECT hostName() AS h, "
        "if(length(census_instance.instance_id)<=2048, census_instance.instance_id, "
        "'') AS instance_id, "
        "if(length(census_instance.keys_fp)<=2048, census_instance.keys_fp, '') AS keys_fp, "
        "length(census_instance.keys_fp) AS keys_fp_bytes, serving, stopped, "
        "toUnixTimestamp64Milli(census_instance.heartbeat_at) AS generation, "
        "dateDiff('millisecond', census_instance.heartbeat_at, now64(3)) AS age_ms "
        f"FROM {source} AS census_instance LIMIT 1025 BY h"
    )
    return await ch.query_all(complete(sql, sequential=1), params)


def _projected_field(row: dict[str, Any], name: str) -> None:
    value = domain.text(row.get(name), empty=True)
    size = row.get(name + "_bytes")
    if type(size) is not int or not 0 <= size <= domain.FIELD_BYTES or len(value.encode()) != size:
        raise refused("process field exceeds or disagrees with its projection bound")


def _replicas(rows: list[dict[str, Any]], topology: Topology, names: dict[str, str]) -> None:
    counts: dict[str, int] = {}
    identities: set[str] = set()
    for row in rows:
        host = domain.text(row.get("h"))
        if topology.multi and host not in names:
            raise refused("process row came from an unconfigured replica")
        if host not in counts and len(counts) >= (len(names) if topology.multi else 1):
            raise refused("process row came from an extra replica")
        count = counts.get(host, 0) + 1
        if count > 1024:
            raise refused("process replica row limit exceeded")
        counts[host] = count
        identity = domain.identity(row.get("instance_id"), instance=True)
        if identity not in identities and len(identities) >= 1024:
            raise refused("logical observation identity limit exceeded")
        identities.add(identity)


def _collapse_observations(
    rows: list[dict[str, Any]], attempt: domain.Attempt
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    replicas: set[tuple[str, str]] = set()
    for row in rows:
        _projected_field(row, "cell")
        _projected_field(row, "tenancy_mode")
        value = observation(row)
        key = value["instance_id"]
        replica = (row["h"], key)
        if replica in replicas:
            raise refused("one replica returned a duplicated observation")
        replicas.add(replica)
        previous = out.get(key)
        if previous:
            stable = {k: v for k, v in value.items() if k not in {"age_ms", "generation"}}
            old = {k: v for k, v in previous.items() if k not in {"age_ms", "generation"}}
            if stable != old:
                raise refused("replicas disagree on the process observation")
            # Every replica must be fresh; keep the worst observed age for the judge.
            value["age_ms"] = (
                -1
                if min(value["age_ms"], previous["age_ms"]) < 0
                else max(value["age_ms"], previous["age_ms"])
            )
            value["generation"] = max(value["generation"], previous["generation"])
        attempt.retain_observation(value)
        out[key] = value
    return out


def _collapse_instances(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    replicas: set[tuple[str, str]] = set()
    for row in rows:
        key = domain.identity(row.get("instance_id"), instance=True)
        _projected_field(row, "keys_fp")
        generation = row.get("generation")
        value = {
            "keys_fp": domain.text(row.get("keys_fp"), empty=True),
            **{name: row.get(name) for name in ("serving", "stopped", "age_ms", "generation")},
        }
        if (
            any(
                type(value[name]) is not int
                for name in ("serving", "stopped", "age_ms", "generation")
            )
            or value["serving"] not in (0, 1)
            or value["stopped"] not in (0, 1)
            or type(generation) is not int
            or generation < 0
        ):
            raise refused("unreadable web instance metadata")
        replica = (row["h"], key)
        if replica in replicas:
            raise refused("one replica returned a duplicated web instance")
        replicas.add(replica)
        previous = out.get(key)
        if previous:
            if any(previous[name] != value[name] for name in ("keys_fp", "serving", "stopped")):
                raise refused("replicas disagree on a web instance")
            value["age_ms"] = (
                -1
                if min(value["age_ms"], previous["age_ms"]) < 0
                else max(value["age_ms"], previous["age_ms"])
            )
            value["generation"] = max(value["generation"], previous["generation"])
        out[key] = value
    return out


@dataclass(frozen=True)
class Snapshot:
    """Complete selected evidence with explicit unresolved obligations; no activation."""

    participants: dict[str, dict[str, Any]]
    observations: dict[str, dict[str, Any]]
    pending: tuple[str, ...]
    keys_fp: str | None
    raw_bytes: int
    retained_bytes: int


async def read_cell(
    ch: Any, attempt: domain.Attempt, context: domain.Context, *, deadline: float
) -> Snapshot:
    """Read participants, all observations/instances, then participants again."""
    context.validate()
    if attempt.cell_name is not None and attempt.cell_name != context.cell_name:
        raise refused("census attempt cannot change its cell")
    attempt.cell_name = context.cell_name
    reader = _Reads(ch, attempt, deadline)
    first = await provider_participants.census(reader, bounded=True)
    attempt.capture(first.rows)
    await _catalog(reader, first.topology, first.names)
    raw_observations = await _observations(reader, first.topology)
    _replicas(raw_observations, first.topology, first.names)
    raw_instances = await _instances(reader, first.topology)
    _replicas(raw_instances, first.topology, first.names)
    second = await provider_participants.census(reader, bounded=True)
    if (first.topology, first.names, first.engine) != (
        second.topology,
        second.names,
        second.engine,
    ):
        raise refused("participant topology changed during the observation join")
    attempt.capture(second.rows)
    observed = _collapse_observations(raw_observations, attempt)
    instances = _collapse_instances(raw_instances)
    pending, keys_fp = attempt.judge(context, second.rows, observed, instances)
    reader.left()
    return Snapshot(
        second.rows, observed, pending, keys_fp, reader.raw_bytes_used, attempt.retained_bytes
    )


def _finite_fields(value: dict[str, Any]) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            domain.text(item, empty=True)
        elif isinstance(item, dict):
            for key, child in item.items():
                domain.text(key)
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)


def observation(row: dict[str, Any]) -> dict[str, Any]:
    """Decode only the bounded projection; CH evaluates age at the actual read."""
    instance = domain.identity(row.get("instance_id"), instance=True)
    raw = row.get("fingerprint")
    if not isinstance(raw, str) or type(row.get("fingerprint_bytes")) is not int:
        raise refused("unreadable observation fingerprint")
    if (
        row["fingerprint_bytes"] > domain.FINGERPRINT_BYTES
        or len(raw.encode()) != row["fingerprint_bytes"]
    ):
        raise refused("observation fingerprint exceeds its bound")
    value = decode_json(raw)
    if not isinstance(value, dict):
        raise refused("unreadable observation fingerprint")
    fp = {key: value[key] for key in ("v", "fields") if key in value}
    if not cell.readable_fingerprint(fp):
        raise refused("unreadable observation fingerprint")
    # Fingerprint fields use the strict codec and their aggregate byte allowance.
    _finite_fields({key: item for key, item in value.items() if key != "fields"})
    participant = domain.identity(value.get("participant_id"))
    if type(row.get("generation")) is not int or row["generation"] < 0:
        raise refused("unreadable observation generation")
    if type(row.get("age_ms")) is not int:
        raise refused("unreadable observation time")
    return {
        "instance_id": instance,
        "participant_id": participant,
        "cell": domain.text(row.get("cell"), empty=True),
        "mode": domain.text(row.get("tenancy_mode"), empty=True),
        "generation": row["generation"],
        "age_ms": row["age_ms"],
        "value": value,
    }
