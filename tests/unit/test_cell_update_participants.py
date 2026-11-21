"""A finalization census never shrinks by expiry or accepts an old serving identity."""

from __future__ import annotations

import importlib
from uuid import UUID

import pytest


def participant(n, state="running", proof="", version=1, expired=True):
    return {
        "participant_id": str(UUID(int=n)),
        "state": state,
        "host_proof": proof,
        "version": version,
        "expired": expired,
    }


def test_capture_keeps_expired_drained_and_uncertified_retired():
    module = importlib.import_module("server.cli.cell_update_participants")
    values = [participant(1), participant(2, "drained"), participant(3, "retired")]
    assert set(module.capture(values)) == {row["participant_id"] for row in values}


def test_capture_omits_only_complete_foundation_retirement_certificate():
    module = importlib.import_module("server.cli.cell_update_participants")
    good = "host|boot||namespace|1|3|||cell-retirement-v1"
    values = [participant(1, "retired", good), participant(2, "retired", "cell-retirement-v1")]
    assert set(module.capture(values)) == {str(UUID(int=2))}


def test_current_target_in_initial_census_still_must_turn_over():
    module = importlib.import_module("server.cli.cell_update_participants")
    row = participant(1, proof="host|boot||namespace|1|3|||cell-retirement-v1")
    assert row["participant_id"] in module.capture([row])


def test_census_conflicting_equal_versions_refuse_instead_of_picking_a_replica():
    module = importlib.import_module("server.cli.cell_update_participants")
    with pytest.raises(module.cell_control.Refused, match="conflicting"):
        module.latest([participant(1), participant(1, "retired")])


@pytest.mark.parametrize("version", [True, "2", 0, -1, None])
def test_census_refuses_unreadable_versions(version):
    from server.cli import cell_control
    from server.cli import cell_update_participants as module

    with pytest.raises(cell_control.Refused, match="unreadable"):
        module.latest([participant(1, version=version)])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["missing", "drained", "expired", "uncertified", "old-version", "other-proof"]
)
async def test_retirement_requires_permanent_exact_identity_and_newer_version(monkeypatch, change):
    from unittest.mock import AsyncMock

    from server.cli import cell_update_participants as module

    proof = "host|boot||namespace|1|3|||cell-retirement-v1"
    initial = participant(1, proof=proof, version=2)
    current = participant(1, "retired", proof=proof, version=3)
    if change == "missing":
        current = None
    elif change in ("drained", "expired"):
        current["state"] = "drained" if change == "drained" else "running"
    elif change == "uncertified":
        current["host_proof"] = ""
    elif change == "old-version":
        current["version"] = 2
    else:
        current["host_proof"] = proof.replace("|1|3|", "|2|3|")
    monkeypatch.setattr(module, "proves_stopped", lambda _: False)
    ch = type("CH", (), {"query_one": AsyncMock(return_value={"running": 0})})()
    assert not await module.stopped(ch, initial, current)
    ch.query_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_retirement_certificate_is_accepted_without_age_or_query_heuristic():
    from unittest.mock import AsyncMock

    from server.cli import cell_update_participants as module

    proof = "host|boot||namespace|1|3|||cell-retirement-v1"
    ch = type("CH", (), {"query_one": AsyncMock(side_effect=AssertionError("no heuristic"))})()
    assert await module.stopped(
        ch, participant(1, proof=proof), participant(1, "retired", proof, 2)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "running", [None, {}, {"running": "0"}, {"running": False}, {"running": 1}, {"running": 0}]
)
async def test_physical_death_also_requires_known_zero_strict_query_census(monkeypatch, running):
    from unittest.mock import AsyncMock

    from server.cli import cell_update_participants as module

    monkeypatch.setattr(module, "proves_stopped", lambda _: True)
    ch = type("CH", (), {"query_one": AsyncMock(return_value=running)})()
    assert await module.stopped(ch, participant(1), None) is (
        running == {"running": 0} and type(running["running"]) is int
    )
    assert "skip_unavailable_shards=0" in ch.query_one.call_args.args[0]


@pytest.mark.asyncio
async def test_physical_death_query_failure_never_falls_back_to_local(monkeypatch):
    from unittest.mock import AsyncMock

    from server.cli import cell_update_participants as module

    monkeypatch.setattr(module, "proves_stopped", lambda _: True)
    ch = type("CH", (), {"query_one": AsyncMock(side_effect=OSError("replica unavailable"))})()
    with pytest.raises(OSError, match="replica unavailable"):
        await module.stopped(ch, participant(1), None)
    assert ch.query_one.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "valid",
        "worker",
        "captured",
        "retired",
        "unmarked",
        "wrong-cell",
        "old-fingerprint",
        "no-instance",
        "no-participant",
        "not-router",
    ],
)
async def test_replacement_joins_serving_instance_and_guarded_new_participant(monkeypatch, change):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.cli import cell_update_participants as module
    from server.core import cell

    identity = str(UUID(int=2))
    target = cell.as_value({"deployment_id": "dep"})
    proof = "host|boot||namespace|1|3|||cell-retirement-v1"
    rows = {identity: participant(2, proof=proof)}
    captured = {str(UUID(int=1))}
    live = [{"instance_id": "replacement", "serving": change != "worker"}]
    value = {
        "participant_id": identity,
        "fields": target["fields"],
        "serves": True,
        "marked": True,
        "router": True,
    }
    if change == "captured":
        captured.add(identity)
    elif change == "retired":
        rows[identity]["state"] = "retired"
    elif change == "unmarked":
        value["marked"] = False
    elif change == "not-router":
        value["router"] = False
    elif change == "old-fingerprint":
        value["fields"] = {"deployment_id": "old"}
    elif change == "no-instance":
        live = []
    elif change == "no-participant":
        rows = {}
    beat = {
        "instance_id": "replacement",
        "cell": "other" if change == "wrong-cell" else "first",
        "tenancy_mode": "enforced",
        "fingerprint": json.dumps(value),
    }
    monkeypatch.setattr(module.instances, "read_live", AsyncMock(return_value=live))
    ch = SimpleNamespace(query_all=AsyncMock(return_value=[beat]))
    run = SimpleNamespace(args=SimpleNamespace(cell="first"), row={"is_first": True})
    assert await module.replacement(ch, run, rows, captured, target) is (change == "valid")
    assert "use_query_cache = '0'" in ch.query_all.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine", ["ReplacingMergeTree", "ReplicatedReplacingMergeTree", "SharedReplacingMergeTree"]
)
async def test_census_syncs_before_complete_replica_union(monkeypatch, engine):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.core.cell_ch import Topology
    from server.insights.store import provider_participants as module

    events = []
    topology = Topology("replicas", 1, (("a", 9000), ("b", 9000)))
    monkeypatch.setattr(module, "read_topology", AsyncMock(return_value=topology))
    monkeypatch.setattr(
        module.cell_catalog, "host_names", AsyncMock(return_value={"a": "a", "b": "b"})
    )
    monkeypatch.setattr(module.cell_catalog, "own_refusals", AsyncMock(return_value=([], [])))

    async def per_host(ch, top, names, sql, params):
        if "source:tables" in sql:
            return {name: [{"engine": engine}] for name in names}
        return {
            name: [
                {
                    "database": "loom",
                    "table": "_provider_participants",
                    "zookeeper_path": "/same",
                    "replica_name": name,
                    "replica_is_active": {"a": 1, "b": 1},
                }
            ]
            for name in names
        }

    async def sync(*args):
        events.append("sync")

    async def query(sql, params):
        events.append("read")
        assert "clusterAllReplicas" in sql
        assert "skip_unavailable_shards=0" in sql
        assert "result_overflow_mode = 'throw'" in sql
        assert "use_query_cache = '0'" in sql
        assert f"select_sequential_consistency={int(engine.startswith('Shared'))}" in sql
        return [participant(1), participant(2), participant(1, "drained", version=2)]

    monkeypatch.setattr(module, "per_host", per_host)
    monkeypatch.setattr(module.cell_sync, "_system", sync)
    result = await module.census(SimpleNamespace(query_all=query))
    assert len(result) == 2 and result[str(UUID(int=1))]["state"] == "drained"
    assert events == (["sync", "read"] if engine.startswith("Replicated") else ["read"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem",
    ["unlisted", "shards", "missing-table", "unreachable", "extra-replica", "sync-failure"],
)
async def test_incomplete_topology_or_sync_never_returns_a_partial_census(monkeypatch, problem):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.core.cell_ch import Topology
    from server.insights.store import provider_participants as module

    topology = Topology(
        "replicas",
        0 if problem == "unlisted" else 2 if problem == "shards" else 1,
        () if problem == "unlisted" else (("a", 9000), ("b", 9000)),
    )
    monkeypatch.setattr(module, "read_topology", AsyncMock(return_value=topology))
    monkeypatch.setattr(
        module.cell_catalog, "host_names", AsyncMock(return_value={"a": "a", "b": "b"})
    )
    monkeypatch.setattr(module.cell_catalog, "own_refusals", AsyncMock(return_value=([], [])))

    async def per_host(*args):
        if problem == "unreachable":
            raise OSError("replica unavailable")
        if "source:tables" in args[3]:
            return {
                "a": [{"engine": "ReplicatedReplacingMergeTree"}],
                "b": []
                if problem == "missing-table"
                else [{"engine": "ReplicatedReplacingMergeTree"}],
            }
        return {
            name: [
                {
                    "database": "loom",
                    "table": "_provider_participants",
                    "zookeeper_path": "/same",
                    "replica_name": name,
                    "replica_is_active": {
                        "a": 1,
                        "b": 1,
                        **({"unknown": 1} if problem == "extra-replica" else {}),
                    },
                }
            ]
            for name in ("a", "b")
        }

    monkeypatch.setattr(module, "per_host", per_host)
    monkeypatch.setattr(
        module.cell_sync, "_system", AsyncMock(side_effect=OSError("cannot synchronize"))
    )
    query = AsyncMock(side_effect=AssertionError("must not read partial census"))
    with pytest.raises((module.Refused, OSError)):
        await module.census(SimpleNamespace(query_all=query))
    query.assert_not_awaited()
