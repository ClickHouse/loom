"""The bounded reader evaluates owned KeeperMap rows through real ClickHouse I/O."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import time
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from server.core import cell
from server.core.cell_ch import Refused
from server.core.ch import CHClient, query_tag
from server.insights.domain.cell_observations import Attempt, Context
from server.insights.store.cell_observations import read_cell
from server.settings import settings

KEYS = "a" * 64
PROOF = "host|boot|container|namespace|123|456|||cell-retirement-v1"
SERVER = UUID(int=1)
WORKER = UUID(int=2)
_COLUMNS = {
    "_provider_participants": (
        "participant_id UUID, state LowCardinality(String), version UInt64, host_proof String"
    ),
    "cell_heartbeats": (
        "instance_id String, cell String DEFAULT '', tenancy_mode String DEFAULT '', "
        "fingerprint String DEFAULT '', heartbeat_at DateTime64(3) DEFAULT now64(3)"
    ),
    "instances": (
        "instance_id String, deployment UInt8 DEFAULT 0, label String DEFAULT '', "
        "host String DEFAULT '', pid UInt32 DEFAULT 0, build_commit String DEFAULT '', "
        "schema_version UInt32 DEFAULT 0, serving UInt8 DEFAULT 1, workers_lease String DEFAULT '', "
        "settings_fp String DEFAULT '', keys_fp String DEFAULT '', config_fp String DEFAULT '', "
        "started_at DateTime64(3) DEFAULT now64(3), "
        "heartbeat_at DateTime64(3) DEFAULT now64(3), stopped UInt8 DEFAULT 0"
    ),
}


@pytest.fixture
async def census_store(monkeypatch):
    if not os.environ.get("LOOM_CH_HOST"):
        pytest.skip("Set LOOM_CH_HOST to the owned disposable ClickHouse")
    database = "census_" + uuid4().hex
    client = CHClient()
    cleanup = contextlib.AsyncExitStack()

    async def drop_owned():
        await client.execute("DROP DATABASE {db:Identifier} SYNC", {"db": database})
        assert await client.query_one(
            "SELECT count() AS n FROM system.tables WHERE database={db:String}",
            {"db": database},
        ) == {"n": 0}
        assert await client.query_one(
            "SELECT count() AS n FROM system.databases WHERE name={db:String}",
            {"db": database},
        ) == {"n": 0}

    with query_tag("test.cell.observation_census"):
        try:
            cleanup.push_async_callback(client.disconnect)
            await client.connect()
            await client.execute("CREATE DATABASE {db:Identifier}", {"db": database})
            cleanup.push_async_callback(drop_owned)
            for table, columns in _COLUMNS.items():
                params = {"db": database, "table": table}
                if table == "_provider_participants":
                    engine = "ReplacingMergeTree(version) ORDER BY participant_id"
                else:
                    engine = "KeeperMap({root:String}) PRIMARY KEY instance_id"
                    params["root"] = f"/loom_it_census/{database}/{table}"
                await client.execute(
                    f"CREATE TABLE {{db:Identifier}}.{{table:Identifier}} ({columns}) "
                    f"ENGINE = {engine}",
                    params,
                )
                assert await client.query_one(
                    "SELECT count() AS n FROM {db:Identifier}.{table:Identifier}", params
                ) == {"n": 0}
            catalog = await client.query_all(
                "SELECT name, engine, engine_full FROM system.tables "
                "WHERE database={db:String} ORDER BY name",
                {"db": database},
            )
            assert {row["name"] for row in catalog} == set(_COLUMNS)
            for row in catalog:
                if row["name"] == "_provider_participants":
                    assert row["engine"] == "ReplacingMergeTree"
                else:
                    assert row["engine"] == "KeeperMap"
                    assert f"/loom_it_census/{database}/{row['name']}" in row["engine_full"]
            monkeypatch.setattr(settings, "ch_database", database)
            monkeypatch.setattr(settings, "ch_cluster", "")
            yield client
        except BaseException as first:  # Keep setup/body failure through owned cleanup.
            try:
                await cleanup.aclose()
            except BaseException as error:  # noqa: BLE001 — the first failure remains primary.
                first.add_note(f"Owned census cleanup also failed: {type(error).__name__}")
            raise
        else:
            await cleanup.aclose()


def context():
    fingerprint = cell.as_value({"deployment_id": "dep", "oidc_client_id": "x" * 3000})
    assert cell.readable_fingerprint(fingerprint)
    return Context("later", False, (fingerprint,), (fingerprint,), KEYS)


def participant(identity, *, state="running", version=1):
    return {
        "participant_id": identity,
        "state": state,
        "version": version,
        "host_proof": PROOF,
    }


def observation(identity, role, configured, *, extra=None):
    value = copy.deepcopy(configured.own_pair[0])
    value.update(
        participant_id=str(identity),
        marked=True,
        serves=role == "server",
        router=False,
        attach_observation={
            "protocol": 1,
            "role": role,
            "participant_id": str(identity),
            "host_proof": PROOF,
            "keys_fp": KEYS,
            "accepting": True,
            "period_ms": 60000,
        },
    )
    value.update(extra or {})
    return {
        "instance_id": identity.hex,
        "cell": configured.cell_name,
        "tenancy_mode": "enforced",
        "fingerprint": json.dumps(value),
    }


async def seed(client, configured, *, worker_observation=True):
    await client.insert("loom._provider_participants", [participant(SERVER), participant(WORKER)])
    rows = [observation(SERVER, "server", configured)]
    if worker_observation:
        rows.append(observation(WORKER, "worker", configured))
    await client.insert("loom.cell_heartbeats", rows)
    await client.insert("loom.instances", [{"instance_id": SERVER.hex, "keys_fp": KEYS}])


async def test_native_reader_preserves_large_fields_and_refuses_other_oversized_metadata(
    census_store,
):
    client, configured, attempt = census_store, context(), Attempt()
    await seed(client, configured)
    deadline = time.monotonic() + 30
    snapshot = await read_cell(client, attempt, configured, deadline=deadline)
    assert snapshot.pending == () and snapshot.keys_fp == KEYS
    assert set(snapshot.participants) == {str(SERVER), str(WORKER)}
    assert {
        value["value"]["attach_observation"]["role"] for value in snapshot.observations.values()
    } == {"server", "worker"}
    assert 0 < snapshot.raw_bytes <= 8 * 1024**2
    assert 0 < snapshot.retained_bytes <= 8 * 1024**2
    for value in snapshot.observations.values():
        assert value["value"]["fields"]["oidc_client_id"] == "x" * 3000
        assert type(value["generation"]) is int and value["generation"] > 0
        assert type(value["age_ms"]) is int and 0 <= value["age_ms"] <= 180000
    await client.insert(
        "loom.cell_heartbeats",
        [observation(WORKER, "worker", configured, extra={"metadata_note": "x" * 2049})],
    )
    with pytest.raises(Refused, match="process metadata exceeds its field bound"):
        await read_cell(client, attempt, configured, deadline=deadline)


async def test_native_retirement_closes_only_the_missing_worker_in_the_same_attempt(census_store):
    client, configured, attempt = census_store, context(), Attempt()
    await seed(client, configured, worker_observation=False)
    deadline = time.monotonic() + 30
    before = await read_cell(client, attempt, configured, deadline=deadline)
    assert before.pending == (str(WORKER),)
    first = copy.deepcopy(attempt.participants[str(WORKER)]["first"])
    birth = attempt.participants[str(WORKER)]["birth"]
    await client.insert(
        "loom._provider_participants", [participant(WORKER, state="retired", version=2)]
    )
    after = await read_cell(client, attempt, configured, deadline=deadline)
    assert after.pending == () and after.keys_fp == KEYS
    assert attempt.participants[str(WORKER)]["closed"] is True
    assert attempt.participants[str(WORKER)]["first"] == first
    assert attempt.participants[str(WORKER)]["birth"] == birth
    assert len(attempt.participants) == 2 and 0 < attempt.retained_bytes <= 8 * 1024**2
    assert attempt.deadline == deadline


@pytest.mark.parametrize("minutes", [-10, 10])
async def test_native_server_clock_marks_stale_or_future_observations_unresolved(
    census_store, minutes
):
    client, configured = census_store, context()
    await seed(client, configured)
    clock = await client.query_one("SELECT now64(3) AS stamp")
    assert clock is not None
    row = observation(WORKER, "worker", configured)
    row["heartbeat_at"] = clock["stamp"] + timedelta(minutes=minutes)
    await client.insert("loom.cell_heartbeats", [row])
    snapshot = await read_cell(client, Attempt(), configured, deadline=time.monotonic() + 30)
    assert snapshot.pending == (str(WORKER),)
    age = snapshot.observations[WORKER.hex]["age_ms"]
    assert age > 180000 if minutes < 0 else age < 0


async def test_native_identity_allowance_retains_closed_history_across_reads(census_store):
    client, configured, attempt = census_store, context(), Attempt()
    await client.insert(
        "loom._provider_participants",
        [participant(SERVER)] + [participant(UUID(int=n), state="retired") for n in range(2, 1025)],
    )
    await client.insert("loom.cell_heartbeats", [observation(SERVER, "server", configured)])
    await client.insert("loom.instances", [{"instance_id": SERVER.hex, "keys_fp": KEYS}])
    deadline = time.monotonic() + 30
    snapshot = await read_cell(client, attempt, configured, deadline=deadline)
    assert snapshot.pending == () and len(attempt.participants) == 1024
    retained = copy.deepcopy(attempt.participants)
    retained_bytes = attempt.retained_bytes
    await client.execute("TRUNCATE TABLE loom._provider_participants")
    await client.insert(
        "loom._provider_participants",
        [participant(SERVER), participant(UUID(int=1025), state="retired")],
    )
    with pytest.raises(Refused, match="cumulative participant identity limit exceeded"):
        await read_cell(client, attempt, configured, deadline=deadline)
    assert attempt.participants == retained and attempt.retained_bytes == retained_bytes
    assert attempt.deadline == deadline
