"""Real SCIM and source publishers cannot outlive a competing raw-history cleanup."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.auth import person_erasure, scim
from server.core import leases
from server.core.errors import OverloadedError
from server.erasure.app import crawl_person
from server.erasure.store import crawl_controls
from server.knowledge.app import crawl_exclusions
from server.knowledge.app import sources as ops
from server.knowledge.store import sources as source_store
from tests.unit.test_claim_write_settlement import Keeper
from tests.unit.test_crawl_composed_flow import _register
from tests.unit.test_crawl_composed_flow import world as world
from tests.unit.test_crawl_person_controls import _World as ControlRows
from tests.unit.test_crawl_signing_authority import key_view as key_view
from tests.unit.test_google_workspace import account as account
from tests.unit.test_scim import _create, _fresh_world

pytestmark = [pytest.mark.asyncio, pytest.mark.claim_cap]
_REAL_HELD = scim._held
_REAL_HOLDING = leases.holding


class Packet:
    """External receiver: an already-started bounded INSERT or a late refused send."""

    def __init__(self, keeper: Keeper, row: dict[str, Any]):
        self.row = copy.deepcopy(row)
        self.deadline = keeper.send("delayed-metadata")

    def land(self, keeper: Keeper, rows: list[dict[str, Any]]) -> None:
        assert keeper.clock() <= self.deadline + 5, "the server execution limit is retained"
        rows.append(copy.deepcopy(self.row))


async def _sent_or_failed(task: asyncio.Task[Any], sent: asyncio.Event) -> None:
    signal = asyncio.create_task(sent.wait())
    try:
        done, _ = await asyncio.wait({task, signal}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
            pytest.fail("publisher returned without the targeted send")
        assert sent.is_set()
    finally:
        signal.cancel()
        await asyncio.gather(signal, return_exceptions=True)


async def test_cancelled_real_scim_prepared_insert_blocks_finalization_until_safe_expiry(
    monkeypatch: pytest.MonkeyPatch,
):
    with _fresh_world() as state:
        uid = await _create("a@example.com")
        keeper = Keeper(monkeypatch)
        original_insert = state.table.insert
        sent = asyncio.Event()
        packet: Packet | None = None

        @asynccontextmanager
        async def held(name: str):
            async with _REAL_HELD(name):
                state.holding += 1
                try:
                    yield
                finally:
                    state.holding -= 1

        async def insert(table: str, rows: list[dict[str, Any]]):
            nonlocal packet
            if table == scim.TABLE and rows[0]["crawl_erasure_enc"] and packet is None:
                capture = person_erasure.load(uid, rows[0]["crawl_erasure_enc"])
                if capture.prepared:
                    packet = Packet(keeper, rows[0])
                    sent.set()
                    await asyncio.Future()  # The client is cancelled; the receiver still owns it.
            await original_insert(table, rows)

        original_one, original_all = state.table.query_one, state.table.query_all

        async def one(sql: str, params=None):
            if sql.startswith("SELECT count() AS n FROM loom.scim_users"):
                assert params == {"id": uid} and "FINAL" not in sql
                return {
                    "n": sum(
                        row["id"] == uid
                        and (bool(row["crawl_erasure_enc"]) or not row["crawl_erasure_done"])
                        for row in state.table.rows
                    )
                }
            return await original_one(sql, params)

        async def all_rows(sql: str, params):
            if sql.startswith("SELECT DISTINCT crawl_erasure_enc FROM loom.scim_users"):
                assert params["id"] == uid and "FINAL" not in sql
                return [
                    {"crawl_erasure_enc": value}
                    for value in sorted(
                        {
                            row["crawl_erasure_enc"]
                            for row in state.table.rows
                            if row["id"] == uid and row["crawl_erasure_enc"]
                        }
                    )
                ][: params["limit"]]
            if "FROM system.mutations" in sql:
                return [
                    {"is_done": 1, "parts_to_do": 0, "latest_fail_reason": "", "mutation_id": "own"}
                ]
            return await original_all(sql, params)

        async def execute(sql: str, *, params):
            assert "UPDATE crawl_erasure_enc = ''" in sql and params["id"] == uid
            for row in state.table.rows:
                if row["id"] == uid:
                    row.update(
                        crawl_erasure_enc="", crawl_erasure_done=True, crawl_erasure_required=True
                    )

        monkeypatch.setattr(scim, "_held", held)
        monkeypatch.setattr(state.table, "insert", insert)
        monkeypatch.setattr(state.table, "query_one", one)
        monkeypatch.setattr(state.table, "query_all", all_rows)
        monkeypatch.setattr(state.table, "execute", execute, raising=False)
        monkeypatch.setattr(person_erasure, "_finalizer", crawl_person.finalize)
        monkeypatch.setattr(crawl_controls, "_default_client", lambda: state.table)
        task = asyncio.create_task(scim.delete(uid))
        await _sent_or_failed(task, sent)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert packet is not None
        assert not state.table.newest(uid)["crawl_erasure_done"]
        with pytest.raises(scim.Busy):
            await scim.delete(uid)
        assert not state.person_cleanup.finalized
        keeper.clock.advance(19)
        packet.land(keeper, state.table.rows)
        foreign = {**packet.row, "id": "unrelated", "crawl_erasure_enc": "foreign-capsule"}
        state.table.rows.append(copy.deepcopy(foreign))
        keeper.clock.advance(42)
        await scim.delete(uid)
        assert all(
            not row["crawl_erasure_enc"] and row["crawl_erasure_done"]
            for row in state.table.rows
            if row["id"] == uid
        )
        assert state.table.newest(uid)["crawl_erasure_done"]
        assert [row for row in state.table.rows if row["id"] == "unrelated"] == [foreign]


async def test_cancelled_real_registration_cannot_append_after_actual_source_cleanup(
    world,
    monkeypatch: pytest.MonkeyPatch,
):
    keeper = Keeper(monkeypatch)
    original_insert = world.insert
    sent = asyncio.Event()
    packet: Packet | None = None

    @asynccontextmanager
    async def holding(key: str, **kwargs: Any):
        async with _REAL_HOLDING(key, **kwargs) as acquired:
            if acquired:
                async with world.lease(key):
                    yield True
            else:
                yield False

    async def insert(table: str, rows: list[dict[str, Any]]):
        nonlocal packet
        if table == source_store.TABLE and packet is None:
            world.assert_storage_leases()
            packet = Packet(keeper, rows[0])
            sent.set()
            await asyncio.Future()
        await original_insert(table, rows)

    monkeypatch.setattr(leases, "holding", holding)
    monkeypatch.setattr(world, "insert", insert)
    task = asyncio.create_task(_register(world))
    await _sent_or_failed(task, sent)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert packet is not None
    controls = ControlRows()
    controls.rows = []
    complete = False

    async def clean():
        nonlocal complete
        async with crawl_exclusions.org_lease("team", ttl_s=60, wait_s=5):
            async with ops.sources_lease("team", "shared-space"):
                await crawl_controls.controls(
                    "team",
                    "shared-space",
                    str(packet.row["source_id"]),
                    packet.row["registrant_subject"],
                    frozenset({"one@example.com", "person@example.com"}),
                    spellings=(packet.row["registrant_subject"], "person@example.com"),
                    ch=controls,
                )
                complete = True

    with pytest.raises(OverloadedError):
        await clean()
    assert not complete and not controls.executed
    keeper.clock.advance(19)
    packet.land(keeper, controls.rows)
    foreign = {**packet.row, "namespace": "other-space"}
    controls.rows.append(copy.deepcopy(foreign))
    keeper.clock.advance(42)
    await clean()
    assert complete
    [erased] = [row for row in controls.rows if row["namespace"] == "shared-space"]
    assert not erased["active"] and not erased["approval_enc"] and not erased["crawl_generation"]
    assert not erased["registrant_subject"]
    expected_creator = (
        ""
        if packet.row["created_by"] in {packet.row["registrant_subject"], "person@example.com"}
        else packet.row["created_by"]
    )
    assert erased["created_by"] == expected_creator
    assert "one@example.com" not in erased["options_json"]
    assert [row for row in controls.rows if row["namespace"] == "other-space"] == [foreign]
