"""The complete owning reader uses real SQL and the bounded raw transport on fakes."""

from __future__ import annotations

import json
import time

import pytest

from server.core.cell_ch import Refused
from server.insights.domain.cell_observations import Attempt
from server.insights.store import cell_observations as store
from server.settings import settings
from tests.unit.test_cell_observation_census import context, instance, observation, participant
from tests.unit.test_cell_scan import FakeCH

HOSTS = ("os-0", "os-1")


def each(rows):
    return [{"h": host, **row} for host in HOSTS for row in rows]


def fixtures(*, problem=""):
    ch = FakeCH()
    ch.on(
        "FROM system.clusters",
        [
            {"host_name": f"ch-{n}", "port": 9000, "shard_num": 1}
            for n in range(65 if problem == "replica-cap" else 2)
        ],
    )
    ch.on("SHOW GRANTS FINAL", [{"g": "GRANT ALL ON *.* TO attach"}])
    ch.on("currentUser()", [{"u": "attach"}])
    ch.on(
        "groupArrayIf(host_name, is_local)",
        [{"h": host, "names": [f"ch-{n}"]} for n, host in enumerate(HOSTS)],
    )
    ch.on("system.processes", each([{"user": "attach"}]))
    ch.on("system.users", each([{"name": "attach"}]))
    ch.on(
        "system.grants",
        each(
            [
                {
                    "user_name": "attach",
                    "role_name": None,
                    "access_type": "ALL",
                    "database": None,
                    "table": None,
                }
            ]
        ),
    )
    ch.on("system.role_grants", [])
    ch.on("system.row_policies", [])
    ch.on("SYSTEM SYNC REPLICA", [{"host": f"ch-{n}", "status": 0} for n in range(2)])
    ch.on(
        "system.replicas",
        [
            {
                "h": host,
                "database": "loom",
                "table": "_provider_participants",
                "zookeeper_path": "/same",
                "replica_name": f"r{n}",
                "replica_is_active": {"r0": 1, "r1": 1},
            }
            for n, host in enumerate(HOSTS)
        ],
    )
    return _data_routes(ch, problem)


def _data_routes(ch, problem):
    ch.on("engine FROM", each([{"engine": "ReplicatedReplacingMergeTree"}]))
    ch.on(
        "engine, engine_full FROM",
        each(
            [
                {
                    "name": name,
                    "engine": "ReplicatedReplacingMergeTree"
                    if name == "_provider_participants"
                    else "KeeperMap",
                    "engine_full": "KeeperMap('/" + name + "')",
                }
                for name in store._TABLES
            ]
        ),
    )
    columns = each(
        [
            {"table": table, "name": name, "type": kind}
            for table, cols in store._COLUMNS.items()
            for name, kind in cols.items()
        ]
    )
    if problem == "column":
        columns[0]["type"] = "String"
    ch.on("system.columns", columns)
    count = 0

    def participants(sql, params):
        nonlocal count
        count += 1
        rows = [participant(), participant(2)]
        if problem == "birth-during-join" and count >= 2:
            rows.append(participant(3))
        if problem == "lower-conflict":
            rows = [
                participant(version=2),
                participant(version=1),
                participant(state="drained", version=1),
            ]
        return each([{**row, "proof_bytes": len(row["host_proof"].encode())} for row in rows])

    ch.on(
        "proof_bytes FROM",
        OSError("replica unavailable") if problem == "unavailable" else participants,
    )
    beats = []
    for value in (observation(), observation(2, role="worker")):
        raw = json.dumps(value["value"])
        beats.append(
            {
                "instance_id": value["instance_id"],
                "cell": "later",
                "cell_bytes": 5,
                "tenancy_mode": "enforced",
                "tenancy_mode_bytes": 8,
                "fingerprint": raw,
                "fingerprint_bytes": len(raw.encode()),
                "age_ms": 0,
                "generation": 1,
            }
        )
    ch.on("`cell_heartbeats`", each(beats))
    ch.on(
        "`instances`",
        each(
            [
                {
                    "instance_id": key,
                    **value,
                    "keys_fp_bytes": len(value["keys_fp"]),
                    "generation": 1,
                }
                for key, value in instance().items()
            ]
        ),
    )
    return ch


async def test_complete_replica_union_reads_workers_and_rechecks_births(monkeypatch):
    monkeypatch.setattr(settings, "ch_cluster", "main")
    ch, attempt = fixtures(), Attempt()
    deadline = time.monotonic() + 10
    result = await store.read_cell(ch, attempt, context(), deadline=deadline)
    assert result.pending == () and result.keys_fp == "a" * 64
    assert len(result.participants) == len(result.observations) == 2
    reads = ch.sql("raw_stream")
    participant_reads = [sql for sql in reads if "proof_bytes FROM" in sql]
    assert len(participant_reads) == 2 and all(" FINAL" not in sql for sql in participant_reads)
    beats = next(sql for sql in reads if "`cell_heartbeats`" in sql)
    assert "dateDiff('millisecond', census_observation.heartbeat_at, now64(3))" in beats
    assert " WHERE " not in beats and "LIMIT 1025 BY h" in beats
    assert all(
        bounds["skip_unavailable_shards"] == 0
        for kind, _, bounds in ch.calls
        if kind == "raw_stream"
    )
    retained = attempt.retained_bytes
    repeated = await store.read_cell(fixtures(), attempt, context(), deadline=deadline)
    assert repeated.pending == () and attempt.retained_bytes == retained
    assert repeated.raw_bytes == result.raw_bytes


async def test_birth_during_join_becomes_an_obligation_not_an_omission(monkeypatch):
    monkeypatch.setattr(settings, "ch_cluster", "main")
    ch, attempt = fixtures(problem="birth-during-join"), Attempt()
    result = await store.read_cell(ch, attempt, context(), deadline=time.monotonic() + 10)
    extra = participant(3)["participant_id"]
    assert extra in result.pending and extra in attempt.participants
    assert len(result.participants) == 3


@pytest.mark.parametrize("problem", ["replica-cap", "column", "lower-conflict", "unavailable"])
async def test_unverified_complete_census_never_returns_a_snapshot(monkeypatch, problem):
    monkeypatch.setattr(settings, "ch_cluster", "main")
    ch = fixtures(problem=problem)
    with pytest.raises(OSError if problem == "unavailable" else Refused):
        await store.read_cell(ch, Attempt(), context(), deadline=time.monotonic() + 10)
    # A failed all-replica participant read is never repeated against just this server.
    if problem == "unavailable":
        reads = [sql for sql in ch.sql("raw_stream") if "proof_bytes FROM" in sql]
        assert len(reads) == 1 and "clusterAllReplicas" in reads[0]


@pytest.mark.parametrize("field", ["oidc_client_id", "future"])
async def test_complete_reader_accepts_matching_large_fingerprint_fields(monkeypatch, field):
    from dataclasses import replace

    from server.core import cell

    monkeypatch.setattr(settings, "ch_cluster", "main")
    accepted = cell.as_value({"deployment_id": "dep", field: "x" * 3000})
    assert cell.readable_fingerprint(accepted)
    assert 2048 < len(accepted["fields"][field].encode()) < 64 * 1024
    configured = replace(context(), own_pair=(accepted,), directory_pair=(accepted,))
    configured.validate()
    ch = fixtures()
    for needle, rows in ch.routes:
        if needle == "`cell_heartbeats`":
            for row in rows:
                value = json.loads(row["fingerprint"])
                value["fields"][field] = accepted["fields"][field]
                row["fingerprint"] = json.dumps(value)
                row["fingerprint_bytes"] = len(row["fingerprint"].encode())
    result = await store.read_cell(ch, Attempt(), configured, deadline=time.monotonic() + 10)
    assert result.pending == () and result.keys_fp == "a" * 64
    assert {
        value["value"]["attach_observation"]["role"] for value in result.observations.values()
    } == {"server", "worker"}
    assert all(
        value["value"]["fields"][field] == accepted["fields"][field]
        for value in result.observations.values()
    )


async def test_complete_reader_still_refuses_oversized_nonfingerprint_metadata(monkeypatch):
    monkeypatch.setattr(settings, "ch_cluster", "main")
    configured = context()
    configured.validate()
    ch = fixtures()
    for needle, rows in ch.routes:
        if needle == "`cell_heartbeats`":
            for row in rows:
                value = json.loads(row["fingerprint"])
                value["metadata_note"] = "x" * 2049
                row["fingerprint"] = json.dumps(value)
                row["fingerprint_bytes"] = len(row["fingerprint"].encode())
    with pytest.raises(Refused, match="process metadata exceeds its field bound"):
        await store.read_cell(ch, Attempt(), configured, deadline=time.monotonic() + 10)
