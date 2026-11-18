"""Analyst grants follow a complete writer fence, never a partial or stale census."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from server.insights.domain.analysis import SettingNames
from server.insights.store import analysis, event_cutover


class WriterWorld:
    def __init__(self) -> None:
        self.servers = [str(UUID(int=1)), str(UUID(int=2))]
        self.topology = [
            dict(
                shard_num=1,
                replica_num=i,
                host_name=f"writer-{i}",
                host_address=f"192.0.2.{i}",
                port=9000,
                user="service",
                default_database="loom",
            )
            for i in (1, 2)
        ]
        self.metadata = [
            dict(
                server_uuid=uid,
                named=1,
                correct=1,
                tables=[
                    ("agent_events", str(UUID(int=10 + i)), "MergeTree"),
                    ("agent_events_private", str(UUID(int=20 + i)), "MergeTree"),
                ],
            )
            for i, uid in enumerate(self.servers)
        ]
        self.process_reads = 0
        self.census_reads = 0
        self.private_after_drain = False
        self.pending_forever = False
        self.change = ""
        self.sent = []
        self.catalog = [(), ()]
        self.observation_cache = {}
        self.observer_servers = list(self.servers)

    @asynccontextmanager
    async def policy_observers(self):
        world = self

        class Observer:
            def __init__(self, index):
                self.index = index
                self.server_uuid = world.observer_servers[index]

            user = "service"
            roles = ()

            async def policies(self):
                return list(world.catalog[self.index])

            async def query_all(self, sql, params=None):
                if "AS topology" in sql:
                    return [
                        {
                            "server_uuid": self.server_uuid,
                            "topology": [tuple(row.values()) for row in world.topology],
                        }
                    ]
                if self.index == 0:
                    world.observation_cache[sql] = await world.query_all(sql, params)
                return copy.deepcopy(
                    [
                        row
                        for row in world.observation_cache[sql]
                        if row["server_uuid"] == self.server_uuid
                    ]
                )

        yield tuple(Observer(index) for index in range(len(self.observer_servers)))

    async def query_all(self, sql, params=None):
        if "FROM system.clusters" in sql:
            return copy.deepcopy(self.topology)
        if sql.startswith("SELECT toString(serverUUID()) AS server_uuid SETTINGS"):
            return [{"server_uuid": self.servers[0]}]
        if "FROM system.tables" in sql:
            return copy.deepcopy(self.metadata)
        if "FROM system.processes" in sql:
            self.process_reads += 1
            if self.process_reads > 1:
                if self.change == "missing_writer":
                    return [{"server_uuid": self.servers[0], "inserts": []}]
                if self.change == "topology":
                    self.topology.pop()
                if self.change == "uuid":
                    self.metadata[0]["tables"][0] = ("agent_events", str(UUID(int=99)), "MergeTree")
                if self.change == "cluster_missing":
                    raise RuntimeError("Code: 701. CLUSTER_DOESNT_EXIST")
            old = (
                [("service", "old-insert")]
                if self.process_reads == 1 or self.pending_forever
                else []
            )
            return [
                {"server_uuid": self.servers[0], "inserts": old},
                # New inserts continue; their identities never join the frozen set.
                {
                    "server_uuid": self.servers[1],
                    "inserts": [("service", "new-insert")] if self.process_reads > 1 else [],
                },
            ]
        if "AS private" in sql:
            self.census_reads += 1
            assert self.process_reads >= 2
            return [
                {"server_uuid": uid, "private": int(self.private_after_drain)}
                for uid in self.servers
            ]
        raise AssertionError(sql)

    async def execute(self, sql, **kwargs):
        self.sent.append(sql)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(event_cutover, "settings", SimpleNamespace(ch_cluster="writers"))
    return WriterWorld()


async def test_the_fence_drains_only_the_frozen_insert_set_before_counting(world):
    await event_cutover.confirm_ordinary_events(ch=world)
    assert world.process_reads == 2 and world.census_reads == 1
    assert world.sent == []


async def test_private_rows_committed_by_an_old_insert_preserve_data_and_refuse_grants(world):
    world.private_after_drain = True
    statements = ["CREATE USER loom_analyst", "GRANT SELECT ON loom.agent_events TO loom_analyst"]
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await analysis.write_entities(statements, names=SettingNames("SQL_"), ch=world)
    assert world.census_reads == 1 and world.sent == [] and world.private_after_drain


@pytest.mark.parametrize("change", ["missing_writer", "topology", "uuid", "cluster_missing"])
async def test_writer_or_table_drift_never_counts_as_old_insert_completion(world, change):
    world.change = change
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world)
    assert world.census_reads == 0


@pytest.mark.parametrize(
    "defect", ["hidden_table", "missing_check", "wrong_check", "duplicate_writer"]
)
async def test_incomplete_initial_attestation_cannot_start_a_census(world, defect):
    if defect == "hidden_table":
        world.metadata[0]["tables"].pop()
    elif defect == "missing_check":
        world.metadata[0]["named"] = world.metadata[0]["correct"] = 0
    elif defect == "wrong_check":
        world.metadata[0]["correct"] = 0
    else:
        world.metadata[1]["server_uuid"] = world.metadata[0]["server_uuid"]
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world)
    assert world.process_reads == world.census_reads == 0


async def test_cutover_cancellation_is_not_a_success_or_a_retry(world, monkeypatch):
    original = world.query_all

    async def query(sql, params=None):
        if "FROM system.processes" in sql:
            raise asyncio.CancelledError
        return await original(sql, params)

    monkeypatch.setattr(world, "query_all", query)
    with pytest.raises(asyncio.CancelledError):
        await event_cutover.confirm_ordinary_events(ch=world)
    assert world.census_reads == 0 and world.sent == []


async def test_grants_follow_the_completed_census(world):
    statements = ["GRANT SELECT ON loom.agent_events TO loom_analyst"]
    await analysis.write_entities(statements, names=SettingNames("SQL_"), ch=world)
    assert world.census_reads == 1 and world.sent == statements


async def test_a_surviving_old_insert_exhausts_the_budget_without_grants(world, monkeypatch):
    world.pending_forever = True
    monkeypatch.setattr(event_cutover, "_BUDGET_S", 0.02)
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world)
    assert world.process_reads > 0 and world.census_reads == 0 and world.sent == []


@pytest.mark.parametrize(
    "target", ["loom.agent_events", "system.processes", "system.row_policies", "loom.*"]
)
async def test_filtered_empty_results_with_valid_writer_uuids_cannot_authorize_grants(
    world, target
):
    # The normal SELECT interface can report clean aggregate UUID rows. The native
    # access catalog still reveals the policy that made its population invisible.
    world.catalog[1] = (
        f"CREATE ROW POLICY hidden_rows ON {target} AS RESTRICTIVE FOR SELECT USING 0 TO ALL",
    )
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await analysis.write_entities(
            ["GRANT SELECT ON loom.agent_events TO loom_analyst"],
            names=SettingNames("SQL_"),
            ch=world,
        )
    assert world.process_reads == world.census_reads == 0
    assert not world.sent


@pytest.mark.parametrize("defect", ["missing", "duplicate", "extra", "unexpected"])
async def test_direct_observer_routes_must_cover_each_actual_writer_exactly_once(world, defect):
    if defect == "missing":
        world.observer_servers.pop()
    elif defect == "duplicate":
        world.observer_servers[1] = world.observer_servers[0]
    elif defect == "extra":
        world.observer_servers.append(str(UUID(int=3)))
    else:
        world.observer_servers[1] = str(UUID(int=3))
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world)
    assert world.process_reads == world.census_reads == 0 and not world.sent


async def test_policy_drift_during_the_old_insert_drain_cannot_reuse_clean_visibility(
    world, monkeypatch
):
    original = world.query_all

    async def change_catalog(sql, params=None):
        rows = await original(sql, params)
        if "FROM system.processes" in sql:
            world.catalog[1] = (
                "CREATE ROW POLICY hidden ON system.processes AS RESTRICTIVE FOR SELECT USING 0 TO ALL",
            )
        return rows

    monkeypatch.setattr(world, "query_all", change_catalog)
    with pytest.raises(event_cutover.EventCutoverUnavailable):
        await event_cutover.confirm_ordinary_events(ch=world)
    assert world.census_reads == 0 and not world.sent
